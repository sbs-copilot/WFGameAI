<template>
  <el-card class="task-info-card">
    <template #header>
      <el-page-header @back="back">
        <template #content>
          <h3>OCR 识别报告</h3>
        </template>
      </el-page-header>
    </template>
    <el-scrollbar ref="scrollbarRef" class="h-full">
      <el-descriptions title="▪️任务信息" :column="1" border>
        <template #extra>
          <el-button type="warning" plain @click="downloadTask(task)"
            >下载报告</el-button
          >
        </template>
        <el-descriptions-item
          label="任务ID"
          label-class-name="desc-label"
          width="80px"
        >
          <span class="font-bold">{{ task?.id || "-" }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="状态" label-class-name="desc-label">
          <el-tag
            :type="getEnumEntry(taskStatusEnum, task?.status)?.type || 'info'"
            size="small"
          >
            {{ getLabel(taskStatusEnum, task?.status) }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="数据来源" label-class-name="desc-label">
          <div class="source-info">
            <el-tag
              :type="task?.source_type === 'git' ? 'success' : 'info'"
              size="small"
              class="source-type-tag"
            >
              {{ task?.source_type === "git" ? "Git" : "上传" }}
            </el-tag>
            <span>{{ getSourceDisplay(task) }}</span>
          </div>
        </el-descriptions-item>
        <el-descriptions-item label="识别阈值" label-class-name="desc-label">{{
          task?.config?.rec_score_thresh ?? "-"
        }}</el-descriptions-item>
        <el-descriptions-item label="耗时" label-class-name="desc-label">{{
          task?.duration || "-"
        }}</el-descriptions-item>
        <el-descriptions-item label="图片总数" label-class-name="desc-label">{{
          task?.total_images
        }}</el-descriptions-item>
        <el-descriptions-item label="命中数" label-class-name="desc-label">{{
          task?.matched_images
        }}</el-descriptions-item>
        <el-descriptions-item label="匹配率" label-class-name="desc-label"
          >{{ task?.match_rate || 0 }}%</el-descriptions-item
        >
        <el-descriptions-item label="创建者" label-class-name="desc-label">{{
          task?.creator_name || "-"
        }}</el-descriptions-item>
        <el-descriptions-item label="创建时间" label-class-name="desc-label">{{
          TimeDefault(task?.created_at)
        }}</el-descriptions-item>
        <el-descriptions-item
          label="备注"
          :span="2"
          label-class-name="desc-label"
        >
          <span
            :class="{
              'text-red-400': task?.remark
            }"
          >
            {{ task?.remark || "-" }}
          </span>
        </el-descriptions-item>
      </el-descriptions>

      <div class="mt-4" v-if="task">
        <el-descriptions title="▪️翻译资源" :column="1" border>
          <template #extra>
            <div class="flex items-center justify-between">
              <el-checkbox v-model="localShowTranslation" size="small">
                显示翻译资源
              </el-checkbox>
              <el-tooltip
                content="下载配置的翻译仓库，并自动检查当前识别图片是否存在对应的翻译版本。"
                placement="top"
              >
                <el-button
                  class="ml-3"
                  type="warning"
                  plain
                  @click="openBindDialog"
                >
                  {{ task?.config?.trans_repo ? "修改关联" : "关联翻译资源" }}
                </el-button>
              </el-tooltip>
              <el-button
                v-if="task?.config?.trans_repo"
                type="danger"
                plain
                @click="handleUnbind"
              >
                解除关联
              </el-button>
            </div>
          </template>
          <template v-if="task?.config?.trans_repo">
            <el-descriptions-item
              v-if="task.config.trans_repo.status"
              label="关联状态"
              label-class-name="desc-label"
              width="60px"
            >
              <el-tag
                v-if="transRepoStatusMap[task.config.trans_repo.status]"
                :type="transRepoStatusMap[task.config.trans_repo.status].type"
                effect="light"
              >
                <el-icon
                  v-if="
                    transRepoStatusMap[task.config.trans_repo.status].loading
                  "
                  class="is-loading mr-1"
                  ><Loading
                /></el-icon>
                {{ transRepoStatusMap[task.config.trans_repo.status].label }}
              </el-tag>
              <span v-else>{{ task.config.trans_repo.status }}</span>
            </el-descriptions-item>

            <el-descriptions-item
              v-if="
                task.config.trans_repo.status === 'failed' &&
                task.config.trans_repo.error
              "
              label="错误信息"
              label-class-name="desc-label"
              width="60px"
            >
              <span class="text-red-500 break-all">{{
                task.config.trans_repo.error
              }}</span>
            </el-descriptions-item>

            <el-descriptions-item
              label="仓库地址"
              label-class-name="desc-label"
              width="60px"
            >
              <div class="break-all">{{ task.config.trans_repo.url }}</div>
            </el-descriptions-item>
            <el-descriptions-item
              label="仓库分支"
              label-class-name="desc-label"
              width="60px"
            >
              {{ task.config.trans_repo.branch }}
            </el-descriptions-item>
            <el-descriptions-item
              label="目录映射"
              label-class-name="desc-label"
              width="60px"
            >
              <div
                v-for="(m, idx) in task.config.trans_repo.mapping"
                :key="idx"
              >
                {{ m.source_subdir }}
                <el-icon><Right /></el-icon> {{ m.trans_subdir }}
              </div>
            </el-descriptions-item>
          </template>
          <template v-else>
            <el-descriptions-item label="状态" label-class-name="desc-label">
              <span class="text-gray-400">未关联</span>
            </el-descriptions-item>
          </template>
        </el-descriptions>
      </div>

      <div class="mt-4">
        <div class="font-bold mb-2">任务参数</div>
        <pre class="json-content">{{ getTaskConfigDisplay(task?.config) }}</pre>
      </div>
    </el-scrollbar>
    <OcrTranslationBindDialog
      v-if="task?.id"
      v-model="bindDialogVisible"
      :task-id="task.id"
      :task-config="task.config"
      @success="handleBindSuccess"
    />
  </el-card>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, computed } from "vue";
import { type ElScrollbar } from "element-plus";
import { type OcrTask } from "@/api/ocr";
import { taskStatusEnum, getEnumEntry, getLabel } from "@/utils/enums";
import { TimeDefault } from "@/utils/time";
import { ocrTaskApi } from "@/api/ocr";
import { superRequest } from "@/utils/request";
import { useOcr } from "../../list/utils/hook";
import { Right, Loading } from "@element-plus/icons-vue";
import OcrTranslationBindDialog from "./OcrTranslationBindDialog.vue";
import { ElMessageBox, ElMessage } from "element-plus";
import { useNavigate } from "@/views/common/utils/navHook";
import { useSSE, SSEEvent } from "@/layout/components/sseState/useSSE";
import { debounce } from "@/utils/utils";

const { on } = useSSE();
// 监听 OCR 任务更新事件
on(SSEEvent.OCR_TASK_UPDATE, (_data: OcrTask) => {
  debounce(refresh, 100)();
});

const { router } = useNavigate();
const back = () => {
  router.back();
};

const transRepoStatusMap: Record<
  string,
  {
    type: "success" | "primary" | "info" | "danger";
    label: string;
    loading?: boolean;
  }
> = {
  binding: { type: "primary", label: "关联中...", loading: true },
  failed: { type: "danger", label: "关联失败" },
  completed: { type: "success", label: "已关联" }
};

const { downloadTask } = useOcr();

const props = defineProps<{
  taskId: string;
  showTranslation?: boolean;
}>();

const emit = defineEmits(["refresh", "update:showTranslation"]);
const bindDialogVisible = ref(false);

const localShowTranslation = computed({
  get: () => props.showTranslation || false,
  set: val => emit("update:showTranslation", val)
});

const openBindDialog = () => {
  bindDialogVisible.value = true;
};

const handleBindSuccess = () => {
  refresh();
  emit("refresh");
};

const handleUnbind = async () => {
  try {
    await ElMessageBox.confirm(
      "确定要解除翻译资源关联吗？这将清除所有已匹配的翻译图片信息。",
      "提示",
      {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "warning"
      }
    );

    await superRequest({
      apiFunc: ocrTaskApi.unbindTransRepo,
      apiParams: props.taskId,
      onSucceed: () => {
        ElMessage.success("解除关联成功");
        refresh();
        emit("refresh");
      }
    });
  } catch (e) {
    // cancel or error
  }
};

const scrollbarRef = ref<InstanceType<typeof ElScrollbar> | null>(null);
const task = ref<OcrTask | null>(null);
const getSourceDisplay = (task: OcrTask) => {
  if (task?.source_type === "git") {
    return `${task?.git_repository_url} ${
      task?.git_branch ? `(${task?.git_branch})` : ""
    }`;
  }
  return "文件上传";
};

const getTaskConfigDisplay = (config: any) => {
  if (!config) return "-";
  const { trans_repo, ...rest } = config;
  console.log("task trans_repo:", trans_repo);
  return JSON.stringify(rest, null, 2);
};

const refresh = async () => {
  if (!props.taskId) return;
  try {
    const res = await superRequest({
      apiFunc: ocrTaskApi.get,
      apiParams: props.taskId
    });
    console.log("task details:", res.data);
    task.value = res?.data || null;
    nextTick(() => {
      scrollbarRef.value?.setScrollTop(0);
    });
  } catch (error) {
    console.error(error);
  }
};

watch(
  () => props.taskId,
  newId => {
    if (newId) {
      refresh();
    } else {
      task.value = null;
    }
  },
  { immediate: true }
);
</script>

<style lang="scss" scoped>
.task-info-card {
  display: flex;
  flex-direction: column;
  justify-items: center;
  height: 100%;

  .el-card__body {
    flex: 1;
    min-height: 0;
  }

  .source-info {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }

  .desc-label {
    cursor: pointer;
  }

  .json-content {
    background-color: #fafafa;
    padding: 10px;
    border-radius: 4px;
    font-family: Consolas, Monaco, monospace;
    font-size: 12px;
    line-height: 1.5;
    color: #606266;
    border: 1px solid #ebeef5;
    margin: 0;
    white-space: pre-wrap;
    word-break: break-all;
  }
}
</style>
