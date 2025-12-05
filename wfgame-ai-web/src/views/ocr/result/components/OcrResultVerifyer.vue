<template>
  <div v-if="visible" class="ocr-verifyer-container">
    <el-image-viewer
      v-if="results.length > 0"
      ref="imageViewerRef"
      :url-list="urlList"
      :initial-index="currentIndex"
      @close="handleClose"
      @switch="handleSwitch"
      :hide-on-click-modal="false"
    />

    <!-- 自定义覆盖层 -->
    <Teleport to="body">
      <div class="verify-overlay" v-if="visible && currentResult">
        <!-- 右侧侧边栏布局 -->
        <div class="right-sidebar">
          <!-- 顶部固定信息 -->
          <div class="top-info-section">
            <div class="image-info">
              <div class="current-index">当前第 {{ currentIndex + 1 }} 张</div>
              <div class="file-path">{{ currentResult.image_path }}</div>
            </div>
          </div>

          <!-- 中间区域 -->
          <div class="middle-section">
            <div class="ocr-content-header">
              <h3>{{ panelTitle }}</h3>
            </div>

            <el-scrollbar class="text-list-scroll" @wheel.stop>
              <div class="text-list">
                <div
                  v-for="(text, index) in currentDisplayTexts"
                  :key="index"
                  class="text-item-row"
                  :class="{ 'is-editing': editingIndex === index }"
                >
                  <el-tag size="small" type="info" class="text-index-tag">{{
                    index + 1
                  }}</el-tag>

                  <!-- 查看模式 -->
                  <div v-if="editingIndex !== index" class="text-view-mode">
                    <span class="text-value">{{ text }}</span>
                    <!-- 矫正模式下的操作按钮 -->
                    <div v-if="isCorrecting" class="item-hover-actions">
                      <el-button
                        link
                        type="primary"
                        :icon="Edit"
                        @click="enableEdit(index)"
                      />
                      <el-button
                        link
                        type="danger"
                        :icon="Delete"
                        @click="deleteItem(index)"
                      />
                    </div>
                  </div>

                  <!-- 编辑模式 -->
                  <div v-else class="text-edit-mode">
                    <el-input
                      v-model="tempEditText"
                      type="textarea"
                      :rows="2"
                      placeholder="请输入文本"
                      @keydown.enter.ctrl.prevent="saveEdit"
                    />
                    <div class="edit-actions">
                      <el-button
                        link
                        type="primary"
                        size="small"
                        @click="saveEdit"
                        >确定</el-button
                      >
                      <el-button link size="small" @click="cancelEdit"
                        >取消</el-button
                      >
                    </div>
                  </div>
                </div>
                <div
                  v-if="currentDisplayTexts.length === 0"
                  class="text-content-empty"
                >
                  （无文本）
                </div>
              </div>
            </el-scrollbar>

            <!-- 矫正模式下的底部操作 -->
            <div v-if="isCorrecting" class="correction-footer">
              <el-button text bg :icon="Plus" @click="addItem" class="add-btn"
                >添加文本块</el-button
              >
              <div class="correction-global-actions">
                <el-button @click="cancelCorrection">取消操作</el-button>

                <el-button type="primary" @click="submitCorrection"
                  >确认提交</el-button
                >
              </div>
            </div>
          </div>

          <!-- 底部操作栏 -->
          <div class="control-section">
            <div class="progress-info">
              <div class="progress-text">{{ progressFormat() }}</div>
              <el-progress
                :percentage="progressPercentage"
                :show-text="false"
                :stroke-width="12"
                striped
                :striped-flow="false"
              />
            </div>

            <div class="actions" v-if="!isCorrecting">
              <div class="action-btn-group">
                <el-button
                  type="success"
                  circle
                  size="large"
                  class="big-circle-btn"
                  @click="handleVerify(ocrResultTypeEnum.RIGHT.value)"
                >
                  <el-icon :size="32"><Check /></el-icon>
                </el-button>
                <span class="btn-label">正确</span>
              </div>

              <div class="action-btn-group">
                <el-button
                  type="danger"
                  circle
                  size="large"
                  class="big-circle-btn"
                  @click="startCorrection(ocrResultTypeEnum.WRONG.value)"
                >
                  <el-icon :size="32"><Close /></el-icon>
                </el-button>
                <span class="btn-label">误检</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div
        v-if="visible && loading && results.length === 0"
        class="loading-mask"
      >
        <el-icon class="is-loading"><Loading /></el-icon> 加载中...
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted, nextTick } from "vue";
import { ElImageViewer, ElMessage } from "element-plus";
import {
  Loading,
  Check,
  Close,
  Delete,
  Plus,
  Edit
} from "@element-plus/icons-vue";
import {
  type OcrResult,
  ocrTaskApi,
  ocrResultApi,
  type TaskGetDetailsParams
} from "@/api/ocr";
import { ocrResultTypeEnum } from "@/utils/enums";
import { mediaUrl } from "@/api/utils";

const props = defineProps<{
  taskId: string;
  visible: boolean;
}>();

const emit = defineEmits<{
  (e: "update:visible", value: boolean): void;
  (e: "refresh"): void;
}>();

const loading = ref(false);
const results = ref<OcrResult[]>([]);
const currentIndex = ref(0);
const pagination = ref({
  currentPage: 1,
  pageSize: 25,
  total: 0
});
const initialTotal = ref(0);
const verifiedCount = ref(0);
const imageViewerRef = ref();

// 矫正相关状态
// 矫正相关状态
const isCorrecting = ref(false);
const correctionType = ref<number>(0);
const correctionTexts = ref<string[]>([]);
const editingIndex = ref<number | null>(null);
const tempEditText = ref("");

// 计算属性
const urlList = computed(() =>
  results.value.map(item => mediaUrl(item.image_path))
);
const currentResult = computed(() => results.value[currentIndex.value]);
const currentDisplayTexts = computed(() => {
  if (isCorrecting.value) {
    return correctionTexts.value;
  }
  return currentResult.value?.texts || [];
});

const panelTitle = computed(() => {
  if (!isCorrecting.value) return "识别文本";
  return correctionType.value === ocrResultTypeEnum.WRONG.value
    ? "更正文本"
    : "补全文本";
});

const progressPercentage = computed(() => {
  if (initialTotal.value === 0) return 0;
  return Math.min(
    Math.round((verifiedCount.value / initialTotal.value) * 100),
    100
  );
});

const progressFormat = () => {
  return `已校验 ${verifiedCount.value} / 总待校验 ${initialTotal.value}`;
};

const fetchResults = async (append = false) => {
  if (loading.value) return;
  loading.value = true;

  try {
    const params: TaskGetDetailsParams = {
      id: props.taskId,
      result_type: ocrResultTypeEnum.UNCHECK.value, // 0
      page: 1,
      page_size: pagination.value.pageSize
    };

    const res = await ocrTaskApi.getDetails(params);
    if (res.code === 0) {
      const { results: newResults, total } = res.data;

      if (!append && initialTotal.value === 0) {
        initialTotal.value = total;
      }

      if (append) {
        const existingIds = new Set(results.value.map(r => r.id));
        const uniqueNewResults = newResults.filter(r => !existingIds.has(r.id));
        if (uniqueNewResults.length > 0) {
          results.value = [...results.value, ...uniqueNewResults];
        } else if (total === 0) {
          ElMessage.success("所有图片已审核完毕！");
        }
      } else {
        results.value = newResults;
        if (newResults.length === 0 && total === 0) {
          ElMessage.info("当前没有待审核的图片");
          handleClose();
        }
      }

      pagination.value.total = total;
    }
  } catch (error) {
    console.error(error);
    ElMessage.error("获取数据失败");
  } finally {
    loading.value = false;
  }
};

const handleSwitch = (index: number) => {
  currentIndex.value = index;
  // 重置矫正状态
  cancelCorrection();

  if (index >= results.value.length - 2) {
    fetchResults(true);
  }
};

const startCorrection = (type: number) => {
  correctionType.value = type;
  isCorrecting.value = true;
  editingIndex.value = null;
  tempEditText.value = "";

  if (type === ocrResultTypeEnum.WRONG.value) {
    correctionTexts.value = currentResult.value?.texts
      ? [...currentResult.value.texts]
      : [];
    if (correctionTexts.value.length === 0) {
      correctionTexts.value.push("");
      enableEdit(0);
    }
  } else {
    correctionTexts.value = [""];
    enableEdit(0);
  }
};

const cancelCorrection = () => {
  isCorrecting.value = false;
  correctionTexts.value = [];
  editingIndex.value = null;
  tempEditText.value = "";
};

const enableEdit = (index: number) => {
  editingIndex.value = index;
  tempEditText.value = correctionTexts.value[index];
  nextTick(() => {
    const input = document.querySelector(
      ".text-edit-mode .el-textarea__inner"
    ) as HTMLTextAreaElement;
    if (input) input.focus();
  });
};

const saveEdit = () => {
  if (editingIndex.value !== null) {
    correctionTexts.value[editingIndex.value] = tempEditText.value;
    editingIndex.value = null;
    tempEditText.value = "";
  }
};

const cancelEdit = () => {
  // 如果是新增的空行被取消，且不是唯一一行，则删除
  if (
    editingIndex.value !== null &&
    correctionTexts.value[editingIndex.value] === "" &&
    correctionTexts.value.length > 1
  ) {
    correctionTexts.value.splice(editingIndex.value, 1);
  }
  editingIndex.value = null;
  tempEditText.value = "";
};

const addItem = () => {
  correctionTexts.value.push("");
  enableEdit(correctionTexts.value.length - 1);
};

const deleteItem = (index: number) => {
  correctionTexts.value.splice(index, 1);
  if (editingIndex.value === index) {
    editingIndex.value = null;
  } else if (editingIndex.value !== null && editingIndex.value > index) {
    editingIndex.value--;
  }
};

const submitCorrection = async () => {
  const validTexts = correctionTexts.value
    .map(t => t.trim())
    .filter(t => t.length > 0);

  if (validTexts.length === 0) {
    ElMessage.warning("内容不能为空");
    return;
  }

  if (correctionType.value === ocrResultTypeEnum.WRONG.value) {
    const currentTexts = currentResult.value?.texts || [];
    if (
      validTexts.length === currentTexts.length &&
      validTexts.every((val, index) => val === currentTexts[index])
    ) {
      ElMessage.warning("当前输入的文本与识别结果相同");
      return;
    }
  }

  await handleVerify(correctionType.value, validTexts);
  cancelCorrection();
};

const handleVerify = async (type: number, correctedTexts?: string[]) => {
  if (!currentResult.value) return;

  const result = currentResult.value;

  try {
    const params: any = {
      id: result.id,
      task_id: props.taskId,
      result_type: type
    };
    if (correctedTexts) {
      params.corrected_texts = correctedTexts;
    }
    await ocrResultApi.verify(params);

    verifiedCount.value++;

    if (currentIndex.value < results.value.length - 1) {
      imageViewerRef.value?.setActiveItem(currentIndex.value + 1);
    } else {
      await fetchResults(true);
      if (currentIndex.value < results.value.length - 1) {
        imageViewerRef.value?.setActiveItem(currentIndex.value + 1);
      } else {
        ElMessage.success("本批次审核完成");
      }
    }
  } catch (e) {
    console.error(e);
    ElMessage.error("操作失败");
  }
};

const handleClose = () => {
  emit("update:visible", false);
  emit("refresh");
};

watch(
  () => props.visible,
  val => {
    if (val) {
      document.body.classList.add("ocr-verify-mode");
      results.value = [];
      currentIndex.value = 0;
      pagination.value.currentPage = 1;
      initialTotal.value = 0;
      verifiedCount.value = 0;
      cancelCorrection();
      fetchResults();

      window.addEventListener("keydown", handleKeydown);
    } else {
      document.body.classList.remove("ocr-verify-mode");
      window.removeEventListener("keydown", handleKeydown);
    }
  }
);

const handleKeydown = (e: KeyboardEvent) => {
  // 如果正在输入，不处理快捷键
  if (isCorrecting.value) return;

  if (e.key === "Enter") {
    const target = e.target as HTMLElement;
    if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;

    handleVerify(ocrResultTypeEnum.RIGHT.value);
  }
};

onUnmounted(() => {
  document.body.classList.remove("ocr-verify-mode");
  window.removeEventListener("keydown", handleKeydown);
});
</script>

<style>
/* 全局样式，用于调整 el-image-viewer 布局以适应侧边栏 */
.ocr-verify-mode .el-image-viewer__next {
  right: 420px !important;
}
.ocr-verify-mode .el-image-viewer__close {
  right: 420px !important;
}
.ocr-verify-mode .el-image-viewer__canvas {
  width: calc(100% - 400px) !important;
}
</style>

<style scoped>
.ocr-verifyer-container {
}

.verify-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 3000;
  display: flex;
  justify-content: flex-end; /* 靠右布局 */
}

.right-sidebar {
  width: 400px;
  height: 100%;
  background: rgba(0, 0, 0, 0.65);
  backdrop-filter: blur(10px);
  pointer-events: auto;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 20px;
  box-sizing: border-box;
}

.top-info-section {
  flex-shrink: 0;
  margin-bottom: 15px;
}

.middle-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  margin-bottom: 15px;
  background: rgba(255, 255, 255, 0.95);
  border-radius: 8px;
}

.ocr-content-header {
  padding: 15px 15px 10px;
  flex-shrink: 0;
  border-bottom: 1px solid #eee;
}

.ocr-content-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: bold;
  color: #000;
}

.text-list-scroll {
  flex: 1;
  overflow: hidden;
}

.text-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 15px;
}

.text-item-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px;
  border-bottom: 1px dashed #eee;
  border-radius: 4px;
  transition: background-color 0.2s;
}

.text-item-row:hover {
  background-color: #f5f7fa;
}

.text-item-row:last-child {
  border-bottom: none;
}

.text-index-tag {
  flex-shrink: 0;
  margin-top: 2px;
}

.text-view-mode {
  flex: 1;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
}

.text-value {
  word-break: break-all;
  font-size: 14px;
  line-height: 1.5;
  flex: 1;
}

.item-hover-actions {
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
}

.text-item-row:hover .item-hover-actions {
  opacity: 1;
}

.text-edit-mode {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.text-content-empty {
  color: #999;
  font-style: italic;
  text-align: center;
  padding: 10px 0;
}

.correction-footer {
  flex-shrink: 0;
  padding: 15px;
  border-top: 1px solid #eee;
  background: #fff;
}

.add-btn {
  width: 100%;
  margin-bottom: 10px;
}

.correction-global-actions {
  display: flex;
  justify-content: space-between;
  gap: 10px;
}

.control-section {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.2);
  padding-top: 20px;
}

.progress-info {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.progress-text {
  font-size: 16px;
  font-weight: bold;
  color: #fff;
  text-align: center;
}

.actions {
  display: flex;
  justify-content: space-around;
  align-items: flex-start;
  color: #fff;
}

.action-btn-group {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.big-circle-btn {
  width: 64px !important;
  height: 64px !important;
  font-size: 24px !important;
}

.btn-label {
  font-size: 14px;
  font-weight: 500;
}

.loading-mask {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.7);
  z-index: 3001;
  display: flex;
  justify-content: center;
  align-items: center;
  color: #fff;
  font-size: 20px;
}

.image-info {
  background: rgba(255, 255, 255, 0.1);
  padding: 12px;
  border-radius: 6px;
  color: #ddd;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.current-index {
  font-size: 18px;
  font-weight: bold;
  color: #409eff;
}

.file-path {
  font-size: 12px;
  word-break: break-all;
  opacity: 0.8;
  line-height: 1.4;
}

@keyframes fadeIn {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
