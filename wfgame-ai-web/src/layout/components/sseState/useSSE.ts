import { ref, onUnmounted, readonly } from "vue";
import { baseUrlApi } from "@/api/utils";
import { getToken } from "@/utils/auth";
import { useUserStoreHook } from "@/store/modules/user";
// import { message } from "@/utils/message";

// 定义事件处理函数的类型
type EventHandler<T = any> = (data: T) => void;

// 定义所有事件监听器的集合类型
// 键是事件名 (如 'message', 'broadcast')，值是该事件的处理函数数组
type Listeners = {
  [event: string]: EventHandler[];
};

// 连接状态的枚举
export enum SSEState {
  CONNECTING = 0,
  OPEN = 1,
  CLOSED = 2
}

export enum SSEEvent {
  MESSAGE = "message", // 私信消息
  BROADCAST = "broadcast", // 广播消息
  CONNECTION_ESTABLISHED = "connection_established", // 连接建立确认
  HEARTBEAT = "heartbeat", // 心跳
  NOTIFICATION = "notification", // 通用弹窗消息
  // ============= 更多自定义业务事件 =============
  OCR_TASK_UPDATE = "ocr_task_update", // OCR 任务更新
  ACTION_UPDATE = "action_update", // 动作库更新
  DEVICE_UPDATE = "device_update", // 设备信息更新
  OCR_REPORT_EXPORT = "ocr_report_export" // OCR离线报告导出
}

// --- 模块级变量，确保全局只有一个 EventSource 实例和监听器集合 ---

// EventSource 实例的引用
let eventSource: EventSource | null = null;

// 所有组件注册的事件监听器
const listeners: Listeners = {};

// 待处理的事件监听器，用于在 SSE 连接建立前缓存
const pendingListeners = new Set<string>();

// 当前 EventSource 实例上已注册的事件，用于避免重复注册
const registeredEvents = new Set<string>();

// 响应式的连接状态
const sseState = ref<SSEState>(SSEState.CLOSED);

// 自动重连的定时器
let reconnectTimer: NodeJS.Timeout | null = null;
const reconnectInterval = 5000; // 5秒后重连

// SSE 连接的 URL，请根据你的项目配置修改
const getSSEUrl = (): string => {
  const baseUrl = baseUrlApi("/notifications/stream/");
  const tokenData = getToken();

  if (tokenData?.accessToken) {
    // 检查 token 是否过期
    const now = new Date().getTime();
    const expired = parseInt(tokenData.expires) - now <= 0;

    if (!expired) {
      const separator = baseUrl.includes("?") ? "&" : "?";
      return `${baseUrl}${separator}token=${encodeURIComponent(
        tokenData.accessToken
      )}`;
    }
  }

  return baseUrl;
};

/**
 * 检查并刷新 token，然后重新连接 SSE
 */
const checkTokenAndReconnect = async (): Promise<boolean> => {
  const tokenData = getToken();

  if (!tokenData) {
    console.log("No token available, cannot establish SSE connection");
    return false;
  }

  const now = new Date().getTime();
  const expired = parseInt(tokenData.expires) - now <= 0;

  if (expired && tokenData.refreshToken) {
    try {
      console.log("Token expired, refreshing...");
      await useUserStoreHook().handRefreshToken({
        refreshToken: tokenData.refreshToken
      });
      console.log("Token refreshed successfully");
      return true;
    } catch (error) {
      console.error("Failed to refresh token:", error);
      return false;
    }
  }

  return true;
};
/**
 * 初始化并管理全局唯一的 SSE 连接。
 * 这个函数只应在应用的主入口（如 App.vue）调用一次。
 */
const connect = async () => {
  // 如果已经连接或正在连接，则不执行任何操作
  if (eventSource && eventSource.readyState !== EventSource.CLOSED) {
    return;
  }

  // 清除可能存在的重连定时器
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  // 检查并刷新 token
  const tokenValid = await checkTokenAndReconnect();
  if (!tokenValid) {
    console.log("Token validation failed, SSE connection aborted");
    return;
  }

  try {
    const sseUrl = getSSEUrl();
    console.log(
      "Connecting to SSE:",
      sseUrl.replace(/token=[^&]+/, "token=***")
    ); // 隐藏token日志

    eventSource = new EventSource(sseUrl, { withCredentials: true });
    sseState.value = SSEState.CONNECTING;

    // 清空已注册事件的追踪，因为这是一个新的 EventSource 实例
    registeredEvents.clear();

    eventSource.onopen = () => {
      sseState.value = SSEState.OPEN;
      console.log("SSE connection established.");
    };

    eventSource.onerror = error => {
      console.error("SSE connection error:", error);
      sseState.value = SSEState.CLOSED;

      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }

      // 401 错误可能是 token 过期，尝试重新连接
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => {
          console.log("Attempting to reconnect SSE...");
          connect();
        }, reconnectInterval);
      }

      // message("与服务端的 SSE 通信断连，正在尝试重新连接...", {
      //   type: "warning"
      // });
    };

    // 这个 onmessage 用于捕获所有没有 'event:' 字段的默认消息
    // 在我们的后端实现中，这个通常不会被用到，但最好有
    eventSource.onmessage = event => {
      console.log("Received default SSE message:", event.data);
    };

    // 监听后端发送的 'connection_established' 事件
    eventSource.addEventListener(SSEEvent.CONNECTION_ESTABLISHED, _event => {
      // message("🔔 与服务端的 SSE 数据流连接成功！", { type: "success" });
      // 连接成功后，重新注册所有已存在的事件监听器
      Object.keys(listeners).forEach(eventName => {
        if (listeners[eventName].length > 0) {
          addSSEEventListener(eventName);
        }
      });
      // 处理所有在连接前注册的事件
      pendingListeners.forEach(eventName => {
        addSSEEventListener(eventName);
      });
      pendingListeners.clear(); // 清空待处理列表
    });
    registeredEvents.add(SSEEvent.CONNECTION_ESTABLISHED);

    // 监听后端发送的 'heartbeat' 事件
    eventSource.addEventListener(SSEEvent.HEARTBEAT, () => {
      // console.log('SSE heartbeat received.'); // 通常不需要打印心跳日志
    });
    registeredEvents.add(SSEEvent.HEARTBEAT);
  } catch (error) {
    console.error("Failed to create EventSource:", error);
    sseState.value = SSEState.CLOSED;
  }
};

/**
 * 关闭 SSE 连接并清空所有监听器。
 */
const disconnect = () => {
  // 清除重连定时器
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  if (eventSource) {
    eventSource.close();
    eventSource = null;
    sseState.value = SSEState.CLOSED;
    // 清空已注册事件的追踪
    registeredEvents.clear();
    // 注意：这里不清空 listeners，保留监听器以便重连时恢复
    console.log("SSE connection closed.");
  }
};

/**
 * 动态地为 EventSource 实例添加事件监听器。
 * @param eventName 事件名称
 */
const addSSEEventListener = (eventName: string) => {
  // 如果 EventSource 实例存在并且连接已打开，则直接添加监听器
  if (eventSource && eventSource.readyState === EventSource.OPEN) {
    // 避免重复注册同一个事件
    if (!registeredEvents.has(eventName)) {
      eventSource.addEventListener(eventName, event => {
        // 检查该事件是否有注册的处理器
        if (listeners[eventName] && listeners[eventName].length > 0) {
          try {
            // 后端发送的数据是 JSON 字符串，需要解析
            const data = JSON.parse((event as MessageEvent).data);
            // 调用所有为该事件注册的处理函数
            listeners[eventName].forEach(handler => handler(data));
          } catch (e) {
            console.error(
              `Error parsing SSE data for event '${eventName}':`,
              e
            );
          }
        }
      });
      registeredEvents.add(eventName);
    }
  } else {
    // 否则，将事件名加入待处理队列
    pendingListeners.add(eventName);
  }
};

/**
 * 手动重新连接 SSE
 */
const reconnect = async () => {
  console.log("Manual reconnection requested");
  disconnect();
  await connect();
};

/**
 * Vue Composable - 用于在组件中使用 SSE。
 * @returns on: 用于注册事件监听器的函数
 * @returns connectionState: 只读的连接状态
 * @returns reconnect: 手动重连函数
 */
export function useSSE() {
  /**
   * 注册一个事件监听器。
   * @param event 事件名称 (例如 'message', 'broadcast')
   * @param handler 事件处理函数
   */
  const on = <T>(event: string, handler: EventHandler<T>) => {
    // 如果这是第一次监听此事件，为 EventSource 创建一个 addEventListener
    if (!listeners[event]) {
      listeners[event] = [];
      addSSEEventListener(event);
    }
    listeners[event].push(handler);

    // 当组件卸载时，自动移除这个监听器，防止内存泄漏
    onUnmounted(() => {
      off(event, handler);
    });
  };

  /**
   * 移除一个事件监听器。
   * @param event 事件名称
   * @param handler 事件处理函数
   */
  const off = <T>(event: string, handler: EventHandler<T>) => {
    if (listeners[event]) {
      const index = listeners[event].indexOf(handler);
      if (index > -1) {
        listeners[event].splice(index, 1);
      }
    }
  };

  return {
    on,
    connectionState: readonly(sseState), // 提供只读的状态
    reconnect // 提供手动重连功能
  };
}

// 导出连接和断开函数，以便在应用根组件中控制
export { connect, disconnect, reconnect };
