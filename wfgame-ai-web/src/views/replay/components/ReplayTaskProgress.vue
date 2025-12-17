<script setup lang="ts">
// 任务进度展示组件（实时）
import { replayApi, scriptApi } from "@/api/scripts";
import { getTaskDetail } from "@/api/tasks";
import CopyToClipboard from "@/components/Common/CopyToClipboard.vue";
import { superRequest } from "@/utils/request";
import { connectSocket } from "@/views/replay/utils/socket";
import { InfoFilled, CircleCheck, CircleClose } from "@element-plus/icons-vue";
import { computed, defineProps, onMounted, onUnmounted, ref, watch } from "vue";

const props = defineProps<{
  taskId: string;
  progress?: number;
  status?: string;
  celeryId?: string;
  deviceIds?: string[]; // 用于计算总步数 = 脚本步数 * 设备数
  scriptIds?: number[]; // 脚本ID列表
}>();

const progressPct = ref<number>(0); // 进度百分比（0-100）
// 固定总步骤：Σ(各脚本步骤数) * 活跃设备数；完成步骤：成功/失败步骤累计
const totalSteps = ref<number>(0);
const finishedSteps = ref<number>(0);
const successSteps = ref<number>(0); // 成功步骤数
const failedSteps = ref<number>(0); // 失败步骤数
// 已完成步骤集合 device|script|stepIndex 用于去重
const finishedKeySet = ref<Set<string>>(new Set());

// 后端提供 completed_steps 与 total_steps 为聚合值，前端不再做均值/拆分计算
const displayFinishedSteps = computed(() => finishedSteps.value);
// 每脚本固定步骤数 script_id -> steps.length
const perScriptStepCount = ref<Record<number, number>>({});
// 活跃设备集合：仅通过快照或 devices_resolved(found_devices) 确认，不直接使用 props.deviceIds 避免计入离线占位
const activeDevices = ref<Set<string>>(new Set());
// 设备离线补偿步数记录：设备ID -> 补偿步数
const offlineCompensatedSteps = ref<Map<string, number>>(new Map());
// 设备离线时间记录：设备ID -> 离线时已完成的步骤数
const deviceOfflineTimes = ref<Map<string, number>>(new Map());
// 设备离线状态记录：设备ID -> 离线时间戳
const deviceOfflineStatus = ref<Map<string, number>>(new Map());
// 是否使用自建步数计算（存在 scriptIds 且设备数>0）
const stepCalcEnabled = ref<boolean>(false);
const runStatus = ref<string>("执行中");
const socketRef = ref<any>(null);
const gotProgress = ref(false);
const celeryId = ref<string | undefined>(props.celeryId);
// no localStorage fallback per requirement

// 状态用于驱动动画（不直接显示文字）

// 外部 props 变更时，同步初始值
watch(
  () => [props.progress, props.status],
  ([p, s]) => {
    if (typeof s === "string" && s) runStatus.value = s;
    if (typeof p === "number") progressPct.value = p;
  }
);

watch(
  () => props.celeryId,
  cid => {
    if (cid) celeryId.value = cid;
  }
);

const recomputePercentFromFinished = () => {
  if (totalSteps.value <= 0) return;
  const pct = Math.round((finishedSteps.value / totalSteps.value) * 100);
  progressPct.value = pct;
};

// 处理设备离线事件，动态调整总步骤数
const handleDeviceOffline = (deviceId: string) => {
  if (!stepCalcEnabled.value) return;
  if (offlineCompensatedSteps.value.has(deviceId)) return; // 已处理

  console.log(`设备 ${deviceId} 离线，正在调整进度计算`);

  // 计算该设备应跑总步数
  const totalForDevice = Object.values(perScriptStepCount.value).reduce((a, b) => a + b, 0);

  // 计算该设备已跑步数
  const deviceCompletedSteps = Array.from(finishedKeySet.value)
    .filter(key => key.startsWith(`${deviceId}|`))
    .length;

  // 计算补偿步数：剩余未跑的步数视为“已处理（失败/跳过）”
  const compensation = Math.max(0, totalForDevice - deviceCompletedSteps);

  offlineCompensatedSteps.value.set(deviceId, compensation);

  // 记录设备离线时的完成状态
  deviceOfflineTimes.value.set(deviceId, deviceCompletedSteps);
  deviceOfflineStatus.value.set(deviceId, Date.now());

  // 从活跃设备列表中移除
  activeDevices.value.delete(deviceId);

  // 重新计算总步骤数（虽然总数应该不变，但触发一下更新）
  recalculateTotalSteps();
  recomputePercentFromFinished();

  console.log(`设备 ${deviceId} 离线，补偿步数: ${compensation}，调整后总步骤: ${totalSteps.value}, 实际完成: ${finishedSteps.value}`);
};

// 处理设备重新上线事件
const handleDeviceOnline = (deviceId: string) => {
  if (!stepCalcEnabled.value) return;

  console.log(`设备 ${deviceId} 重新上线，恢复进度计算`);

  // 清理离线记录和补偿
  deviceOfflineTimes.value.delete(deviceId);
  deviceOfflineStatus.value.delete(deviceId);
  offlineCompensatedSteps.value.delete(deviceId);

  // 重新加入活跃设备列表
  activeDevices.value.add(deviceId);

  // 重新计算总步骤数
  recalculateTotalSteps();
  recomputePercentFromFinished();

  console.log(`设备 ${deviceId} 重新上线，调整后总步骤: ${totalSteps.value}, 已完成: ${finishedSteps.value}`);
};

// 重新计算总步骤数
const recalculateTotalSteps = () => {
  // 已改为后端权威：不再在前端重新计算 aggregate totalSteps
  // 保留函数避免调用报错；仅在 totalSteps 尚未由后端提供时，尝试基于脚本步数与设备数做聚合估算
  if (totalSteps.value > 0) return;
  const scriptStepSum = Object.values(perScriptStepCount.value).reduce((a, b) => a + b, 0);
  if (scriptStepSum <= 0) return;
  let deviceCount = Array.isArray(props.deviceIds) && props.deviceIds.length > 0
    ? props.deviceIds.length
    : Math.max(activeDevices.value.size, 1);
  totalSteps.value = scriptStepSum * deviceCount;
  recomputePercentFromFinished();
};

// 手动调整设备步骤数（用于处理设备异常情况）
const adjustDeviceSteps = (deviceId: string, adjustSteps: number) => {
  if (!stepCalcEnabled.value || !activeDevices.value.has(deviceId)) return;

  console.log(`手动调整设备 ${deviceId} 步骤数: ${adjustSteps}`);

  // 如果是减少步骤数（设备掉线）
  if (adjustSteps < 0) {
    handleDeviceOffline(deviceId);
  } else if (adjustSteps > 0) {
    // 如果是增加步骤数（设备重新上线）
    handleDeviceOnline(deviceId);
  }
};

const registerFinishedStep = (
  device: string | undefined,
  script: any,
  stepIndex: any,
  status: any
) => {
  if (!stepCalcEnabled.value) return;
  const st = String(status || "").toLowerCase();
  if (!["success", "failed"].includes(st)) return;
  const dev = device ? String(device) : "unknown";
  const sid = Number(typeof script === "object" ? script?.id : script);
  const sidx = Number(stepIndex);
  if (!Number.isFinite(sid) || !Number.isFinite(sidx)) return;
  const key = `${dev}|${sid}|${sidx}`;
  if (!finishedKeySet.value.has(key)) {
    finishedKeySet.value.add(key);
    finishedSteps.value = finishedKeySet.value.size;
    recomputePercentFromFinished();
  }
};

const updateProgress = (val?: any) => {
  // 支持多种 payload 结构：{ percent } 或 { current, total }
  let pct: number | null = null;
  if (val && typeof val === "object") {
    if (typeof val.percent === "number") pct = val.percent;
    else if (
      typeof val.current === "number" &&
      typeof val.total === "number" &&
      val.total > 0
    ) {
      pct = Math.round((val.current / val.total) * 100);
    }
  } else if (typeof val === "number") {
    pct = val;
  }
  if (pct != null) progressPct.value = pct;
};

const updateStatus = (val?: any) => {
  // 归一化状态
  const norm = String(val || "").toLowerCase();
  if (["done", "finished", "success", "completed"].includes(norm)) {
    // 仅在所有步骤完成时才将进度强制提升至100%，避免单设备完成就全局100%
    runStatus.value = "完成";
    if (stepCalcEnabled.value) {
      if (finishedSteps.value >= totalSteps.value && totalSteps.value > 0) {
        progressPct.value = 100;
      }
    } else {
      if (progressPct.value < 100) progressPct.value = 100;
    }
  } else if (["failed", "error"].includes(norm)) {
    runStatus.value = "失败";
  } else if (norm) {
    runStatus.value = "执行中";
  }
};

const trySetCeleryId = (data?: any) => {
  if (!data) return;
  let cid: any;
  if (data?.celery_id) cid = data.celery_id;
  else if (data?.celeryId) cid = data.celeryId;
  else if (data?.celeryID) cid = data.celeryID;
  else if (data?.celery_task_id) cid = data.celery_task_id;
  // 兼容部分服务以 task_id 传递 Celery 任务ID（通常为 UUID）
  if (!cid) {
    const maybe = data?.task_id || data?.taskId;
    if (
      maybe &&
      typeof maybe === "string" &&
      maybe.includes("-") &&
      maybe !== String(props.taskId)
    ) {
      cid = maybe;
    }
  }
  if (cid && !celeryId.value) celeryId.value = String(cid);
};

// 监听设备状态变化
const onDeviceStatus = (data: any) => {
  if (!stepCalcEnabled.value) return;

  const deviceId = data?.device || data?.data?.device;
  const status = (data?.status || data?.data?.status || "").toLowerCase();

  if (!deviceId) return;

  console.log(`设备状态变化: ${deviceId} -> ${status}`);

  if (status === "offline" || status === "disconnected") {
    // 设备离线，调整进度计算
    handleDeviceOffline(deviceId);
  } else if (status === "online" || status === "connected") {
    // 设备重新上线，恢复进度计算
    handleDeviceOnline(deviceId);
  }
};

onMounted(() => {
  // 初始化步进式总步数：仅在 scriptIds 与 deviceIds 均可用时启用
  // 步进计算只取决于脚本是否存在；设备数稍后由快照或 devices_resolved 提供
  stepCalcEnabled.value =
    Array.isArray(props.scriptIds) && props.scriptIds.length > 0;
  if (stepCalcEnabled.value) {
    // 暂时不可直接获取每个脚本的步骤数，使用快照加载后填充；此处仅设为0，后续快照归档后补齐
    totalSteps.value = 0;
  }
  // 首屏：不依赖路由/行参数，直接查询后端
  let _initialSet = false;
  // 1) 并发查询：任务详情（判定已完成） + Redis 聚合快照（计算百分比）
  (async () => {
    try {
      // 预先获取所有脚本的步骤数，确保总步数计算准确
      if (stepCalcEnabled.value && Array.isArray(props.scriptIds)) {
        const pssc: Record<number, number> = {};
        // 并发请求所有脚本详情
        const scriptPromises = props.scriptIds.map(sid =>
          scriptApi.detail(sid).catch(() => null)
        );
        const scriptResults = await Promise.all(scriptPromises);

        scriptResults.forEach((res: any) => {
          if (res && (res.data || res)) {
            const s = res.data || res;
            const sid = Number(s.id);
            // 优先使用 steps_count，如果没有则计算 steps 长度
            let count = Number(s.steps_count);
            if (!count && Array.isArray(s.steps)) {
              count = s.steps.length;
            }
            if (Number.isFinite(sid) && count > 0) {
              pssc[sid] = count;
            }
          }
        });

        // 更新每脚本步数缓存
        perScriptStepCount.value = { ...perScriptStepCount.value, ...pssc };

        // 立即计算一次总步数（基于计划设备数）
        recalculateTotalSteps();
      }

      const [detailRes, snapRes] = await Promise.allSettled([
        superRequest({
          apiFunc: getTaskDetail,
          apiParams: String(props.taskId),
          enableFailedMsg: false,
          enableErrorMsg: false
        }),
        replayApi.snapshot({ task_id: String(props.taskId) })
      ]);

      // 解析 snapshot 计算百分比
      let snapPct: number | null = null;
      if (snapRes.status === "fulfilled") {
        const resp: any = snapRes.value as any;
        const data: any = (resp && (resp.data || resp)) || {};
        const devices = Array.isArray(data?.data?.devices)
          ? data.data.devices
          : Array.isArray((data as any)?.devices)
          ? (data as any).devices
          : [];
        let total = 0;
        let completed = 0;
        let success = 0;
        let failed = 0;
        // 若启用脚本单元计算，则通过快照建立脚本步数与已完成集合
        const pssc: Record<number, number> = { ...perScriptStepCount.value }; // 基于已获取的脚本详情初始化
        for (const dev of devices) {
          const records = Array.isArray(dev?.records) ? dev.records : [];
          for (const rec of records) {
            const steps = Array.isArray(rec?.steps) ? rec.steps : [];
            total += steps.length;
            // 统计脚本步数（假设所有设备脚本结构一致）
            const sid = Number(rec?.meta?.id);
            if (Number.isFinite(sid)) {
              // 仅当快照中的步数大于已知的步数时才更新（防止脚本详情获取失败的情况）
              pssc[sid] = Math.max(pssc[sid] || 0, steps.length);
            }
            for (const s of steps) {
              // 兼容新旧结构：优先检查 result.status，其次检查 status
              const st = String(s?.result?.status || s?.status || "").toLowerCase();
              if (st === "success") {
                completed += 1;
                success += 1;
              } else if (st === "failed") {
                completed += 1;
                failed += 1;
              }
              // 步进式填充完成集合（快照）
              if (
                stepCalcEnabled.value &&
                (st === "success" || st === "failed")
              ) {
                const stepIdx = Number(
                  s?.step_index ||
                    s?.index ||
                    (Array.isArray(rec?.steps)
                      ? rec.steps.indexOf(s) + 1
                      : null)
                );
                if (Number.isFinite(stepIdx)) {
                  const key = `${
                    dev?.device || dev?.device_id || dev?.serial || "unknown"
                  }|${sid}|${stepIdx}`;
                  finishedKeySet.value.add(key);
                }
              }
            }
          }
        }
        if (stepCalcEnabled.value) {
          perScriptStepCount.value = pssc;
          const initialActive = new Set<string>();

          // 优先将 props.deviceIds 加入活跃设备，确保初始分母正确
          if (Array.isArray(props.deviceIds)) {
             props.deviceIds.forEach(d => initialActive.add(d));
          }

          for (const dev of devices) {
            const devId = String(
              dev?.device || dev?.device_id || dev?.serial || "unknown"
            );
            if (devId !== "unknown") initialActive.add(devId);
          }
          // 初始快照阶段仅根据真实出现的设备设置活跃设备，避免 props.deviceIds 中的未连接设备占位
          if (initialActive.size > 0) activeDevices.value = initialActive;

          // 重新计算总步数（复用 recalculateTotalSteps 逻辑）
          recalculateTotalSteps();

          finishedSteps.value = finishedKeySet.value.size;
          recomputePercentFromFinished();
        }
        // 更新统计数据
        if (total > 0) {
          totalSteps.value = total;
          snapPct = Math.round((completed / total) * 100);
        }
        if (success > 0) successSteps.value = success;
        if (failed > 0) failedSteps.value = failed;
        if (completed > 0) finishedSteps.value = completed;
      }

      // 解析任务详情状态
      let finished = false;
      if (detailRes.status === "fulfilled") {
        const d: any = (detailRes.value as any) || {};
        const st = String(d?.status || d?.data?.status || "").toLowerCase();
        finished = [
          "done",
          "finished",
          "success",
          "completed",
          "完成",
          "成功"
        ].includes(st);
      }

      if (!gotProgress.value) {
        if (typeof snapPct === "number") {
          // 优先使用任务详情的完成状态
          if (finished) {
            runStatus.value = "完成";
            progressPct.value = 100;
          } else {
            runStatus.value = "执行中";
            progressPct.value = snapPct;
          }
          _initialSet = true;
        } else if (finished) {
          // 兜底：如果任务已完成但快照数据缺失（如Redis过期），强制显示100%
          progressPct.value = 100;
          runStatus.value = "完成";
          _initialSet = true;
        } else {
          progressPct.value = 0;
          runStatus.value = "执行中";
          _initialSet = true;
        }
      }
    } catch (_) {
      if (!gotProgress.value) {
        progressPct.value = 0;
        runStatus.value = "执行中";
      }
    }
  })();

  if (!props.taskId) return;
  socketRef.value = connectSocket(
    { room: `replay_task_${String(props.taskId)}` },
    {
      // 直接监听 progress 事件（后端 HTTP emit）
      onProgress: (data: any) => {
        gotProgress.value = true;
        trySetCeleryId(data);
        
        // 兼容后端推送的多种字段格式
        // 后端推送: { progress, completed_steps, total_steps, success_steps, failed_steps }
        // 旧格式: { percent, current, total }
        const progressValue = data?.progress ?? data?.percent;
        const currentValue = data?.completed_steps ?? data?.current;
        const totalValue = data?.total_steps ?? data?.total;
        const successValue = data?.success_steps;
        const failedValue = data?.failed_steps;
        
        // 同步更新 totalSteps（如果后端提供了）
        if (typeof totalValue === "number" && totalValue > 0) {
          totalSteps.value = totalValue;
        }
        
        // 同步更新成功/失败统计
        if (typeof successValue === "number") {
          successSteps.value = successValue;
        }
        if (typeof failedValue === "number") {
          failedSteps.value = failedValue;
        }
        
        // 优先使用后端推送的进度百分比
        if (typeof progressValue === "number") {
          progressPct.value = progressValue;
          if (progressValue >= 100) updateStatus("finished");
          else updateStatus("running");
        } else if (
          typeof currentValue === "number" &&
          typeof totalValue === "number" &&
          totalValue > 0
        ) {
          const pct = Math.round((currentValue / totalValue) * 100);
          progressPct.value = pct;
          if (pct >= 100) updateStatus("finished");
          else updateStatus("running");
        }
      },
      // 新增：监听 step 事件以便步进式计算
      onStep: (data: any) => {
        // 优先使用后端计算的全局进度（若后端已支持）
        if (typeof data?.progress === "number" && typeof data?.total_steps === "number") {
          progressPct.value = data.progress;
          totalSteps.value = data.total_steps;
          if (typeof data?.completed_steps === "number") finishedSteps.value = data.completed_steps;
          if (typeof data?.success_steps === "number") successSteps.value = data.success_steps;
          if (typeof data?.failed_steps === "number") failedSteps.value = data.failed_steps;
          recomputePercentFromFinished();
          updateStatus(progressPct.value >= 100 ? "finished" : "running");
          return;
        }

        try {
          registerFinishedStep(
            data?.device,
            data?.script,
            data?.step_index,
            data?.status
          );
        } catch (_) {
          /* ignore */
        }
      },
      onSysMsg: (_msg, payload) => {
        const evt = payload?.event || "";
        const data = payload?.data;
        switch (evt) {
          case "devices_resolved": {
            try {
              const found = Array.isArray(data?.found_devices)
                ? data.found_devices.map((d: any) => String(d))
                : [];
              // 仅使用后端真实找到的设备覆盖活跃集合（排除离线）
              activeDevices.value = new Set(found);
              const scriptStepSum = Object.values(
                perScriptStepCount.value
              ).reduce((a, b) => a + b, 0);
              totalSteps.value = scriptStepSum * activeDevices.value.size;
              recomputePercentFromFinished();
            } catch (e) {
              /* ignore */
            }
            break;
          }
          case "device_offline": {
            // 设备离线事件
            const deviceId = data?.device_id || data?.device || data?.serial;
            if (deviceId) {
              handleDeviceOffline(String(deviceId));
            }
            break;
          }
          case "device_online": {
            // 设备重新上线事件
            const deviceId = data?.device_id || data?.device || data?.serial;
            if (deviceId) {
              handleDeviceOnline(String(deviceId));
            }
            break;
          }
          case "device_disconnected": {
            // 设备断开连接事件
            const deviceId = data?.device_id || data?.device || data?.serial;
            if (deviceId) {
              handleDeviceOffline(String(deviceId));
            }
            break;
          }
          case "device_connected": {
            // 设备重新连接事件
            const deviceId = data?.device_id || data?.device || data?.serial;
            if (deviceId) {
              handleDeviceOnline(String(deviceId));
            }
            break;
          }
          case "task_progress":
          case "progress":
          case "progress_update": {
            gotProgress.value = true;
            trySetCeleryId(data);
            if (!stepCalcEnabled.value) {
              updateProgress(data);
              if (typeof data?.percent === "number") {
                if (data.percent >= 100) updateStatus("finished");
                else updateStatus("running");
              } else if (
                typeof data?.current === "number" &&
                typeof data?.total === "number" &&
                data.total > 0
              ) {
                const pct = Math.round((data.current / data.total) * 100);
                if (pct >= 100) updateStatus("finished");
                else updateStatus("running");
              }
            } else {
              if (typeof data?.percent === "number" && data.percent >= 100) {
                if (
                  finishedSteps.value >= totalSteps.value &&
                  totalSteps.value > 0
                ) {
                  updateStatus("finished");
                }
              }
            }
            break;
          }
          case "task_status":
          case "status":
            trySetCeleryId(data);
            updateStatus(data?.status ?? data);
            break;
          case "task_finished":
          case "finished":
            trySetCeleryId(data);
            updateStatus("finished");
            break;
          case "task_error":
            // 错误事件不改变完成计数，但可扩展标记
            break;
          default:
            break;
        }
      }
    }
  );
});

onUnmounted(() => {
  if (socketRef.value) socketRef.value.disconnect();
});
</script>
<template>
  <div class="task-progress">
    <div class="progress-header">
      <h3 class="progress-title">
        执行进度
        <el-tooltip
          content="当前任务执行进度（后端聚合），页面刷新会自动从快照恢复"
          placement="top"
        >
          <el-icon class="progress-help-icon"><InfoFilled /></el-icon>
        </el-tooltip>
      </h3>
      <div class="ids-inline">
        <span class="id-chip">
          任务ID: <strong>{{ props.taskId }}</strong>
        </span>
        <span class="id-chip" v-if="celeryId">
          CeleryID: <strong>{{ celeryId }}</strong>
          <CopyToClipboard :text="String(celeryId)" label="复制 CeleryID" />
        </span>
        <div class="runner" v-if="runStatus === '执行中'" aria-label="running">
          <span class="dot" />
          <span class="dot" />
          <span class="dot" />
        </div>
      </div>
    </div>
    <div class="progress-bar-outer">
      <div class="progress-bar-inner" :style="{ width: progressPct + '%' }">
        <span class="progress-label">{{ progressPct }}%</span>
      </div>
    </div>
    <div v-if="totalSteps > 0" class="step-stats-row">
      <div class="step-stat-item">
        <span class="stat-label">总步数:</span>
        <span class="stat-value">{{ totalSteps }}</span>
        <el-tooltip content="单设备步数 × 在线设备数" placement="top">
          <el-icon class="stat-help-icon"><InfoFilled /></el-icon>
        </el-tooltip>
      </div>
      <div class="step-stat-item success">
        <el-icon><CircleCheck /></el-icon>
        <span class="stat-label">成功:</span>
        <span class="stat-value">{{ successSteps }}</span>
      </div>
      <div class="step-stat-item failed">
        <el-icon><CircleClose /></el-icon>
        <span class="stat-label">失败:</span>
        <span class="stat-value">{{ failedSteps }}</span>
      </div>
      <div class="step-stat-item">
        <span class="stat-label">已完成:</span>
        <span class="stat-value">{{ finishedSteps }} / {{ totalSteps }}</span>
      </div>
    </div>
  </div>
</template>
<style scoped>
.task-progress {
  background: #f5f7fa;
  border-radius: 8px;
  padding: 12px 16px 12px 16px; /* 紧凑 */
  margin-bottom: 12px;
  box-shadow: 0 2px 8px rgba(80, 120, 255, 0.06);
}

.progress-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.progress-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #1e293b;
  display: flex;
  align-items: center;
  gap: 8px;
}

.progress-help-icon {
  font-size: 16px;
  color: #94a3b8;
  cursor: help;
}

.progress-help-icon:hover {
  color: #64748b;
}

.ids-inline {
  display: flex;
  align-items: center;
  gap: 10px;
}

.id-chip {
  background: #f1f5f9;
  color: #475569;
  padding: 5px 12px;
  border-radius: 6px;
  font-size: 14px;
  line-height: 1.2;
  border: 1px solid #e2e8f0;
  box-shadow: inset 0 0 0 1px #ffffffaa;
}

.id-chip strong {
  font-weight: 700;
  color: #0f172a;
  font-size: 15px;
}

/* 删除了头部重复的标题与提示样式 */

.progress-bar-outer {
  width: 100%;
  height: 20px; /* 更低 */
  background: #e0e7ff;
  border-radius: 10px;
  margin: 10px 0 0 0; /* 更紧凑 */
  overflow: hidden;
  box-shadow: 0 1px 4px rgba(80, 120, 255, 0.04);
}

.step-calc-hint {
  margin-top: 6px;
  font-size: 12px;
  color: #64748b;
  text-align: right;
}

.progress-bar-inner {
  height: 100%;
  background: linear-gradient(90deg, #7b8cff 60%, #a5b4fc 100%);
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  transition: width 0.4s;
  position: relative;
}

.progress-label {
  color: #fff;
  font-size: 0.85rem;
  font-weight: bold;
  margin-right: 10px;
  text-shadow: 0 1px 4px #7b8cff44;
}

.step-stats-row {
  display: flex;
  align-items: center;
  gap: 20px;
  margin-top: 12px;
  padding: 8px 12px;
  background: #ffffff;
  border-radius: 6px;
  font-size: 14px;
}

.step-stat-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.step-stat-item.success {
  color: #67c23a;
}

.step-stat-item.success .el-icon {
  font-size: 16px;
}

.step-stat-item.failed {
  color: #f56c6c;
}

.step-stat-item.failed .el-icon {
  font-size: 16px;
}

.stat-label {
  color: #606266;
  font-weight: 500;
}

.stat-value {
  color: #303133;
  font-weight: 600;
}

.stat-help-icon {
  font-size: 14px;
  color: #909399;
  cursor: help;
}

.runner {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #3b82f6;
  animation: dotPulse 1.2s infinite ease-in-out;
}

.dot:nth-child(2) {
  animation-delay: 0.15s;
}
.dot:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes pulse {
  0% {
    box-shadow: 0 0 0 0 #7b8cff44;
  }

  70% {
    box-shadow: 0 0 0 8px #7b8cff11;
  }

  100% {
    box-shadow: 0 0 0 0 #7b8cff44;
  }
}

@keyframes dotPulse {
  0%,
  80%,
  100% {
    transform: scale(0.8);
    opacity: 0.6;
  }
  40% {
    transform: scale(1);
    opacity: 1;
  }
}
</style>
