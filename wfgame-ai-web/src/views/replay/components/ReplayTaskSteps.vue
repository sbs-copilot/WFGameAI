<script setup lang="ts">
// 基于 task.script_ids 循环查询脚本详情，分组展示步骤，并通过 socket 实时更新状态
import { replayApi, scriptApi } from "@/api/scripts"; // 不再依赖 scriptApi.detail 以避免步骤列表不匹配
import { getTaskDetail } from "@/api/tasks";
import CopyToClipboard from "@/components/Common/CopyToClipboard.vue";
import { superRequest } from "@/utils/request";
import { connectSocket } from "@/views/replay/utils/socket";
import { InfoFilled } from "@element-plus/icons-vue";
import { nextTick, onMounted, onUnmounted, ref } from "vue";
// 时间解析：支持毫秒时间戳或 ISO 字符串
const parseToMs = (val: any): number | null => {
  if (val == null) return null;
  if (typeof val === "number" && Number.isFinite(val)) return val; // 已是毫秒
  const s = String(val).trim();
  // ISO 字符串兜底（截断到毫秒 + 时区兼容）
  const m = s.match(/^(.*T\d{2}:\d{2}:\d{2}\.)(\d+)(Z|[+-]\d{2}:?\d{2})?$/);
  let iso = s;
  if (m) {
    const prefix = m[1];
    const frac = m[2].slice(0, 3);
    const tz = m[3] || "Z";
    iso = `${prefix}${frac}${tz}`;
  }
  if (/^\d{4}-\d{2}-\d{2}T/.test(iso) && !/[Z+-]\d{2}:?\d{2}$/.test(iso)) {
    iso += "Z";
  }
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
};
const computeDurationMs = (startVal: any, endVal: any): number | null => {
  const st = parseToMs(startVal);
  const et = parseToMs(endVal);
  if (st === null || et === null) return null;
  return Math.max(0, et - st);
};
const formatDuration = (ms: number): string => {
  const s = ms / 1000;
  return `${s.toFixed(2)}s`;
};

// 判断步骤是否为 defaults 定义（需要过滤掉）
const isDefaultsStep = (step: any): boolean => {
  if (!step || typeof step !== 'object') return false;
  // 检查三种 defaults 标记方式
  return (
    step.step_type === 'defaults' ||
    step.is_defaults === true ||
    step.action === 'set_defaults'
  );
};

const props = defineProps<{ taskId: string; scriptIds?: number[] }>();

type Step = {
  name?: string;
  title?: string;
  remark?: string;
  time?: string;
  status?: string;
  [k: string]: any;
};
type ScriptGroup = {
  scriptId: number;
  scriptName: string;
  expanded: boolean;
  steps: Step[];
};

const loading = ref(false);
const groups = ref<ScriptGroup[]>([]);
const totalSteps = ref(0);
const currentStepNo = ref(0);
const successCount = ref(0);
const failedCount = ref(0);
// 手风琴当前激活的面板名（脚本ID）
const activeName = ref<string | number | null>(null);
const socketRef = ref<any>(null);
// 系统级错误展示：来自 sysMsg(task_error) 或快照的 error_message
const errorAlerts = ref<Array<{ device: string; message: string }>>([]);
const showErrorOverlay = ref(false); // 初次出现错误时，居中提示一次
const showErrorDialog = ref(false); // 查看详情弹窗

function addErrorAlert(device: string, message: string) {
  if (!message) return;
  const before = errorAlerts.value.length;
  errorAlerts.value.push({ device, message });
  if (before === 0) {
    // 首次出现错误：显示居中提示
    showErrorOverlay.value = true;
  }
}

// 使用通用复制组件 CopyToClipboard，移除自建复制函数

const recomputeCounters = () => {
  totalSteps.value = groups.value.reduce(
    (sum, g) => sum + (g.steps?.length || 0),
    0
  );
  const flatSteps = groups.value.flatMap(g => g.steps || []);
  const runningIdx = flatSteps.findIndex(s => s.status === "执行中");
  const waitingIdx = flatSteps.findIndex(s => s.status === "等待");
  if (runningIdx >= 0) currentStepNo.value = runningIdx + 1;
  else if (waitingIdx >= 0) currentStepNo.value = waitingIdx + 1;
  else currentStepNo.value = Math.max(1, flatSteps.length);
  successCount.value = flatSteps.filter(
    s => s.status === "完成" || s.status === "成功"
  ).length;
  failedCount.value = flatSteps.filter(s => s.status === "失败").length;
};

const normalizeStatus = (s: any): string => {
  const v = String(s || "").toLowerCase();
  if (["running", "executing", "processing"].includes(v)) return "执行中";
  if (["done", "finished", "success", "completed"].includes(v)) return "完成";
  if (["failed", "error"].includes(v)) return "失败";
  return "等待";
};

// 将后端的显示态归一化到现有 UI 约定
const mapDisplay = (s: any): string => {
  const v = String(s || "");
  return v === "等待中" ? "等待" : v;
};

onMounted(async () => {
  loading.value = true;
  try {
    // 1) 获取任务详情，提取 script_ids
    let scriptIds: number[] = [];
    await superRequest({
      apiFunc: getTaskDetail,
      apiParams: String(props.taskId),
      enableFailedMsg: false,
      enableErrorMsg: false,
      onSucceed: (data: any) => {
        const list = Array.isArray(data?.script_ids) ? data.script_ids : [];
        for (const it of list) {
          if (it == null) continue;
          // 后端返回的 script_ids 可能是 [1, 2] 或 [{id: 1}, {id: 2}]
          if (typeof it === "object") {
            const v = Number((it as any).id);
            if (Number.isFinite(v)) scriptIds.push(v);
          } else {
            const v = Number(it);
            if (Number.isFinite(v)) scriptIds.push(v);
          }
        }
      }
    });

    // 若外部传入则覆盖
    if (Array.isArray(props.scriptIds) && props.scriptIds.length > 0) {
      scriptIds = props.scriptIds
        .filter(n => Number.isFinite(Number(n)))
        .map(n => Number(n));
    }

    // 2) 仅建立脚本分组（空步骤，后续通过快照或实时事件填充）
    const res: ScriptGroup[] = [];
    for (const sid of scriptIds) {
      const scriptName = `脚本 ${sid}`;
      res.push({ scriptId: sid, scriptName, expanded: false, steps: [] });
    }
    groups.value = res;

    // 尝试获取脚本详情以预填步骤（修复刷新后总步数变少的问题）
    if (scriptIds.length > 0) {
      try {
        const promises = scriptIds.map(sid => scriptApi.detail(sid).catch(() => null));
        const results = await Promise.all(promises);
        results.forEach((r: any) => {
          const s = r?.data || r;
          if (!s) return;
          const sid = Number(s.id);
          const grp = groups.value.find(g => g.scriptId === sid);
          if (grp && Array.isArray(s.steps)) {
            // 更新脚本名
            if (s.name) grp.scriptName = s.name;
            // 预填步骤（过滤掉 defaults 定义）
            const actualSteps = s.steps.filter((step: any) => !isDefaultsStep(step));
            actualSteps.forEach((stepDef: any, idx: number) => {
              if (grp.steps.length <= idx) {
                grp.steps.push({
                  status: "等待",
                  time: "",
                  remark: stepDef.remark || stepDef.name || stepDef.title || `步骤 ${idx + 1}`,
                  name: stepDef.name,
                  title: stepDef.title
                });
              }
            });
          }
        });
      } catch (e) {
        /* ignore */
      }
    }

    recomputeCounters();
    activeName.value = res.length ? res[0].scriptId : null;

    // 3) 订阅 socket 更新（仅使用 replay_step；不再监听 replay_snapshot）
    if (props.taskId) {
      socketRef.value = connectSocket(
        { room: `replay_task_${String(props.taskId)}` },
        {
          // 新增：直接处理 replay_step 推送的数据
          onStep: (data: any) => {
            const scriptInfo = data?.script;
            const targetScript = Number(
              typeof scriptInfo === "object" ? scriptInfo?.id : scriptInfo
            );
            const idx = Number(data?.step_index) - 1;
            const status = normalizeStatus(data?.status);
            const grp = Number.isFinite(targetScript)
              ? groups.value.find(g => g.scriptId === targetScript)
              : groups.value[0];
            if (!grp) return;
            // 若步骤尚不存在，自动补齐占位
            if (Number.isFinite(idx)) {
              while (grp.steps.length <= idx) {
                grp.steps.push({
                  status: "等待",
                  time: "",
                  remark: `步骤 ${grp.steps.length + 1}`
                });
              }
              const target = grp.steps[idx];
              const r =
                data?.result && typeof data.result === "object"
                  ? data.result
                  : {};
              // 优先使用后端的中文显示状态
              const disp = data?.display_status ?? r.display_status;
              target.status = disp ? mapDisplay(disp) : status;

              // 只要存在 end_time 就计算耗时（不再依赖显示状态，避免状态对齐问题）
              const stRaw = r.start_time || data?.start_time || data?.startTime;
              const etRaw = r.end_time || data?.end_time || data?.endTime;
              if (stRaw && etRaw) {
                const ms = computeDurationMs(stRaw, etRaw);
                if (ms !== null) target.time = formatDuration(ms);
              }
            }
            // 自动切换到当前脚本分组并滚动到对应步骤行
            if (grp && status === "执行中") {
              const targetScriptId = grp.scriptId;
              const targetIdx = idx;
              activeName.value = targetScriptId;
              nextTick(() => {
                // 等待折叠面板展开渲染完成再滚动，避免跨脚本时无法定位到第一步
                setTimeout(() => {
                  const el = document.getElementById(
                    `step-${targetScriptId}-${targetIdx}`
                  );
                  if (el && typeof el.scrollIntoView === "function") {
                    el.scrollIntoView({
                      block: "center",
                      behavior: "smooth"
                    });
                  }
                }, 120);
              });
            }
            recomputeCounters();
          },
          onSysMsg: (_msg: string, payload: any) => {
            try {
              const kind = String(payload?.msg || payload?.event || "");
              if (kind === "task_error") {
                const dev = String(payload?.device || "");
                const m = String(
                  payload?.error_message ||
                    payload?.message ||
                    payload?.data?.error_message ||
                    payload?.data?.message ||
                    ""
                );
                addErrorAlert(dev, m);
              } else if (kind === "device_status") {
                const dev = String(
                  payload?.device || payload?.data?.device || ""
                );
                const status = String(
                  payload?.status || payload?.data?.status || ""
                ).toLowerCase();
                const message = String(
                  payload?.message || payload?.data?.message || ""
                );

                if (status === "offline" || status === "unauthorized") {
                  const msg =
                    message ||
                    (status === "unauthorized" ? "设备未授权" : "设备未连接");
                  addErrorAlert(dev, msg);
                }
              }
            } catch (_) {
              /* ignore */
            }
          },
          onError: (err: any) => {
            // 兼容 { data: { message: ... } } 结构
            const payload = (err && err.data) ? err.data : err;
            // 处理后端推送的 error 事件
            // 结构: { message: string, task_id: string, ... }
            if (payload && typeof payload === "object" && payload.message) {
              const dev = payload.device || "任务系统";
              addErrorAlert(String(dev), String(payload.message));
            }
          }
        }
      );
      // 页面加载后主动获取一次 HTTP 快照（Redis 优先，DB 回退）
      try {
        await superRequest({
          apiFunc: replayApi.snapshot,
          apiParams: { task_id: String(props.taskId) },
          enableFailedMsg: false,
          enableErrorMsg: false,
          onSucceed: (resp: any) => {
            const devices = Array.isArray(resp?.data?.devices)
              ? resp.data.devices
              : Array.isArray(resp?.devices)
              ? resp.devices
              : [];
            for (const dev of devices) {
              // 先处理系统级错误信息（若存在）
              if (dev && typeof dev === "object") {
                // 统一错误字段提取逻辑，与 onSysMsg/onError 保持一致
                const err =
                  dev.error_message ||
                  dev.message ||
                  dev.data?.error_message ||
                  dev.data?.message;
                const status = String(dev.status || "").toLowerCase();
                if (err) {
                  addErrorAlert(String(dev.device || ""), String(err));
                } else if (status === "offline") {
                  addErrorAlert(String(dev.device || ""), "设备已离线");
                }
              }
              const records = Array.isArray(dev?.records) ? dev.records : [];
              if (!records.length) continue;
              for (const rec of records) {
                const meta = rec?.meta || {};
                const sid = Number(meta?.id);
                if (!Number.isFinite(sid)) continue;
                let grp = groups.value.find(g => g.scriptId === sid);
                // 若任务详情中未包含该脚本ID（例如进入房间时脚本已完成未更新 script_ids），动态创建分组
                if (!grp) {
                  const scriptName = meta?.name || `脚本 ${sid}`;
                  grp = {
                    scriptId: sid,
                    scriptName,
                    expanded: false,
                    steps: []
                  };
                  groups.value.push(grp);
                  // 若当前无激活分组，则设为激活（进入页面后第一次补齐脚本）
                  if (!activeName.value) activeName.value = sid;
                } else {
                  // 更新已有分组名称（如果任务详情用的是占位名）
                  if (meta?.name && grp.scriptName !== meta.name) {
                    grp.scriptName = meta.name;
                  }
                }
                // 过滤掉 defaults 步骤（快照数据中可能包含 defaults）
                const rawSteps = Array.isArray(rec?.steps) ? rec.steps : [];
                const stepsArr = rawSteps.filter((step: any) => !isDefaultsStep(step));
                
                // 如果快照步骤数少于当前步骤数，删除多余的步骤（避免预填时包含了 defaults）
                if (stepsArr.length < grp.steps.length) {
                  grp.steps.length = stepsArr.length;
                }
                
                for (let i = 0; i < stepsArr.length; i++) {
                  const s = stepsArr[i] || {};
                  let r: any = {};
                  if (s && typeof s === "object" && s.result) {
                    r = s.result;
                  }

                  while (grp.steps.length <= i) {
                    grp.steps.push({
                      status: "等待",
                      time: "",
                      remark: `步骤 ${grp.steps.length + 1}`
                    });
                  }
                  const target = grp.steps[i];
                  const disp = r.display_status ?? s.display_status;
                  target.status = disp
                    ? mapDisplay(disp)
                    : normalizeStatus(r.status ?? s.status);
                  // 补充 remark 展示名称：优先级链条
                  // 1) 顶层步骤定义的 remark
                  // 2) result.remark（兼容旧结构）
                  // 3) 顶层步骤的 name/title
                  // 4) 脚本名 meta.name
                  // 5) 回退 "步骤 i+1"
                  const fallbackName = meta?.name || `脚本 ${sid}`;
                  const remarkVal =
                    s.remark ||
                    r.remark ||
                    s.name ||
                    s.title ||
                    fallbackName ||
                    `步骤 ${i + 1}`;
                  target.remark = remarkVal;
                  const ended =
                    target.status === "完成" ||
                    target.status === "失败" ||
                    target.status === "成功";
                  const stRaw = r.start_time ?? s.start_time;
                  const etRaw = r.end_time ?? s.end_time;
                  if (ended && stRaw && etRaw) {
                    const ms = computeDurationMs(stRaw, etRaw);
                    if (ms !== null) target.time = formatDuration(ms);
                  }
                }
              }
            }
            recomputeCounters();
          }
        });
      } catch (_) {
        // ignore
      }
    }
  } finally {
    loading.value = false;
  }
});

onUnmounted(() => {
  if (socketRef.value) socketRef.value.disconnect();
});
</script>

<template>
  <div class="task-steps">
    <!-- 顶部轻量红色告警条，避免干扰步骤布局 -->
    <div v-if="errorAlerts.length" class="error-banner">
      <span>检测到 {{ errorAlerts.length }} 条系统错误</span>
      <el-button
        size="small"
        type="danger"
        text
        @click="showErrorDialog = true"
      >
        查看详情
      </el-button>
    </div>

    <!-- 首次出现错误时，居中弹出提示卡片，提高可见性 -->
    <div v-if="showErrorOverlay && errorAlerts.length" class="error-overlay">
      <div class="overlay-card">
        <div class="overlay-title">系统错误</div>
        <div class="overlay-body">
          <div class="overlay-msg">
            {{ errorAlerts[0]?.device ? errorAlerts[0].device + "：" : "" }}
            {{ errorAlerts[0]?.message }}
          </div>
        </div>
        <div class="overlay-actions">
          <el-button
            type="danger"
            @click="
              showErrorDialog = true;
              showErrorOverlay = false;
            "
          >
            查看详情
          </el-button>
          <el-button @click="showErrorOverlay = false">我知道了</el-button>
        </div>
      </div>
    </div>
    <div class="steps-header">
      <div class="steps-title-group">
        <h3 class="steps-title">任务执行步骤</h3>
        <el-tooltip content="此处展示首个响应设备的执行进度与步骤详情" placement="top">
          <el-icon class="steps-help-icon"><InfoFilled /></el-icon>
        </el-tooltip>
      </div>

      <div class="steps-stats">
        <span class="steps-badge current">
          当前第 {{ currentStepNo }} / {{ totalSteps }}
        </span>
        <span class="steps-badge success">
          <i class="badge-dot" /> 成功 {{ successCount }}
        </span>
        <span class="steps-badge failed">
          <i class="badge-dot" /> 失败 {{ failedCount }}
        </span>
      </div>
    </div>
    <div v-if="loading" class="loading">正在加载脚本与步骤...</div>
    <div v-else>
      <el-collapse v-model="activeName" accordion class="steps-collapse">
        <el-collapse-item
          v-for="grp in groups"
          :key="grp.scriptId"
          :name="grp.scriptId"
        >
          <template #title>
            <div class="group-header">
              用例ID：{{ grp.scriptId }} ｜ 用例名：{{
                grp.scriptName
              }}
              （步骤：{{ grp.steps.length }}）
            </div>
          </template>
          <ol class="steps-list">
            <li
              v-for="(step, idx) in grp.steps"
              :key="idx"
              class="step-item"
              :id="`step-${grp.scriptId}-${idx}`"
              :class="{ 'running-row': step.status === '执行中' }"
            >
              <span class="step-index">{{ idx + 1 }}</span>
              <span
                class="step-name"
                :title="
                  step.remark || step.name || step.title || `步骤 ${idx + 1}`
                "
              >
                {{
                  step.remark || step.name || step.title || `步骤 ${idx + 1}`
                }}
              </span>
              <span class="step-status">
                <i class="status-dot" :class="step.status" />
                {{ step.status || "等待" }}
              </span>
              <span class="step-time">{{ step.time || "-" }}</span>
            </li>
          </ol>
        </el-collapse-item>
      </el-collapse>
    </div>
  </div>

  <!-- 详情弹窗：按设备列出错误，可复制 -->
  <el-dialog v-model="showErrorDialog" title="系统错误详情" width="640px">
    <div class="error-dialog-list" v-if="errorAlerts.length">
      <div class="dialog-item" v-for="(ea, i) in errorAlerts" :key="'dlg-' + i">
        <div class="dialog-item-left">
          <span class="err-tag">系统错误</span>
          <span class="err-device" v-if="ea.device">{{ ea.device }}：</span>
        </div>
        <div class="dialog-item-right">
          <div class="err-msg">{{ ea.message }}</div>
          <div class="dialog-actions">
            <CopyToClipboard :text="ea.message" :wrap="true">
              <el-button size="small" text>复制</el-button>
            </CopyToClipboard>
          </div>
        </div>
      </div>
    </div>
    <template #footer>
      <el-button @click="showErrorDialog = false">关闭</el-button>
    </template>
  </el-dialog>
</template>
<style scoped>
.task-steps {
  /* 占满父容器（steps-block）的可用高度 */
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  height: 100%;
  /* 由外层 steps-block 提供黑色背景，这里透明以便衬底透出 */
  background: transparent;
  padding: 18px 25px 18px 22px;
  margin: 0;
  position: relative; /* 作为遮罩定位参考 */
}
.steps-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 4px;
}
.steps-title-group {
  display: flex;
  align-items: center;
  gap: 8px;
}
.steps-help-icon {
  color: #94a3b8;
  cursor: help;
  font-size: 15px;
}
.steps-help-icon:hover {
  color: #64748b;
}
.error-banner {
  margin-bottom: 10px;
  background: #fee2e2;
  border: 1px solid #fecaca;
  color: #7a1a1a;
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.err-tag {
  background: #dc2626;
  color: #fff;
  border-radius: 4px;
  padding: 2px 6px;
  font-weight: 600;
}
.err-device {
  font-weight: 600;
}
.err-msg {
  white-space: pre-wrap;
  word-break: break-all;
}

/* 居中提示遮罩 */
.error-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.25);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10;
}
.overlay-card {
  background: #fff;
  border: 1px solid #fecaca;
  border-left: 6px solid #dc2626;
  border-radius: 8px;
  padding: 16px 18px;
  width: min(680px, 90%);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.12);
}
.overlay-title {
  font-weight: 700;
  color: #991b1b;
  margin-bottom: 8px;
}
.overlay-body {
  color: #111827;
  margin-bottom: 12px;
}
.overlay-msg {
  white-space: pre-wrap;
  word-break: break-all;
}
.overlay-actions {
  display: flex;
  gap: 8px;
}

/* 详情弹窗列表 */
.error-dialog-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.dialog-item {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 10px;
  padding: 8px 10px;
  border: 1px dashed #fecaca;
  border-radius: 6px;
  background: #fff7f7;
}
.dialog-item-left {
  display: flex;
  align-items: center;
  gap: 6px;
}
.dialog-item-right {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.dialog-actions {
  white-space: nowrap;
}
.steps-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #1e293b;
}
.steps-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.steps-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.2;
  border: 1px solid transparent;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
  position: relative;
}
.steps-badge .badge-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
  box-shadow: 0 0 0 1px #fff inset;
}
.steps-badge.total {
  background: #e0f2fe;
  color: #0369a1;
  border-color: #bae6fd;
}
.steps-badge.total .badge-dot {
  background: #0284c7;
}
.steps-badge.current {
  background: #ede9fe;
  color: #6d28d9;
  border-color: #ddd6fe;
  font-size: 16px; /* 提升“当前第 X / Y”可读性 */
}
.steps-badge.current .badge-dot {
  background: #7c3aed;
}
.steps-badge.success {
  background: #dcfce7;
  color: #15803d;
  border-color: #bbf7d0;
}
.steps-badge.success .badge-dot {
  background: #16a34a;
}
.steps-badge.failed {
  background: #fee2e2;
  color: #b91c1c;
  border-color: #fecaca;
}
.steps-badge.failed .badge-dot {
  background: #dc2626;
}
.steps-badge.failed {
  animation: badgePulse 2.4s infinite;
}
@keyframes badgePulse {
  0% {
    box-shadow: 0 0 0 0 rgba(220, 38, 38, 0.4);
  }
  70% {
    box-shadow: 0 0 0 6px rgba(220, 38, 38, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(220, 38, 38, 0);
  }
}

/* 复用回放页 chip 风格，并加入浅红色变体 */
.id-chip {
  background: #eef2ff;
  color: #1f2a44;
  padding: 6px 12px;
  border-radius: 8px;
  font-size: 1.05rem;
  font-weight: 600;
  border: 1px solid #c7d2fe;
  box-shadow: 0 1px 3px rgba(80, 120, 255, 0.15);
}
.id-chip.alt {
  background: #ecfeff;
  border-color: #a5f3fc;
}
.id-chip.danger {
  background: #fee2e2; /* 浅红底色 */
  border-color: #fecaca;
  color: #7a1a1a;
}
.card {
  background: #fafcff;
  border: 1px solid #e7efff;
  border-radius: 10px;
  padding: 12px;
  box-shadow: 0 2px 8px rgba(80, 120, 255, 0.06);
}
.group-header {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #3a4a7c;
  font-size: 15px; /* 本行字号一致 */
}
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
.steps-list {
  margin: 8px 0 0 0; /* 减小header与内容间距 */
  padding: 0 0 0 18px; /* 增加缩进 */
}
.step-item {
  display: flex;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid #e0e7ff;
  font-size: 15px; /* 步骤字体稍大 */
  transition: background 0.2s;
}
.step-item.running-row {
  background: #eef2ff; /* 执行中的底色高亮 */
  padding: 10px 0; /* 稍微增大高度 */
  font-size: 16px; /* 提升字号与任务进度一致可读性 */
}
.step-item.running-row .step-index {
  width: 30px;
  height: 30px;
  background: #7b8cff;
  color: #fff;
  font-size: 16px;
  box-shadow: 0 0 0 2px #c7d2fe;
}
.step-item.running-row .step-name,
.step-item.running-row .step-status {
  font-weight: 600;
}
.step-item.running-row .step-time {
  font-weight: 500;
}
.step-item:last-child {
  border-bottom: none;
}
.step-index {
  width: 28px;
  height: 28px;
  background: #e0e7ff;
  color: #7b8cff;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: bold;
  margin-right: 12px;
  font-size: 15px; /* 行内统一字号 */
}
.step-name {
  flex: 1;
  color: #3a4a7c;
  font-size: 15px; /* 步骤字体稍大 */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.step-status {
  min-width: 80px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 15px; /* 行内统一字号 */
}
.status-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 4px;
  background: #bfcfff;
}
.status-dot.完成 {
  background: #22c55e;
}
.status-dot.成功 {
  background: #22c55e;
}
.status-dot.执行中 {
  background: #7b8cff;
  animation: pulse 1.2s infinite;
}
.status-dot.等待 {
  background: #bfcfff;
}
.status-dot.失败 {
  background: #ff4d4f;
}
.step-time {
  min-width: 70px;
  text-align: right;
  color: #64748b;
  font-size: 15px; /* 行内统一字号 */
}
/* Element Plus 折叠面板样式微调 */
.steps-collapse :deep(.el-collapse-item__header) {
  background: #f8fafc;
  padding: 0 12px;
  height: 42px;
}
.steps-collapse {
  /* 让折叠列表充满剩余空间并可滚动 */
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}
.steps-collapse :deep(.el-collapse-item__content) {
  background: transparent; /* 透明以适配父背景 */
  padding: 4px 8px 10px 12px; /* 减少顶部间距 */
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
</style>
