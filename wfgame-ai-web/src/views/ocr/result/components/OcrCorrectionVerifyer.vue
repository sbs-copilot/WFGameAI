<template>
  <div v-if="visible" class="ocr-correction-verifyer-container">
    <el-image-viewer
      v-if="result"
      ref="imageViewerRef"
      :url-list="urlList"
      :initial-index="0"
      @close="handleClose"
      :hide-on-click-modal="false"
    />

    <!-- 自定义覆盖层 -->
    <Teleport to="body">
      <div class="verify-overlay" v-if="visible && result">
        <!-- 右侧侧边栏布局 -->
        <div class="right-sidebar">
          <!-- 顶部固定信息 -->
          <div class="top-info-section">
            <div class="image-info">
              <div class="file-path">{{ result.image_path }}</div>
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
                  v-for="(text, index) in correctionTexts"
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
                    <!-- 操作按钮 -->
                    <div class="item-hover-actions">
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
                  v-if="correctionTexts.length === 0"
                  class="text-content-empty"
                >
                  （无文本，请添加）
                </div>
              </div>
            </el-scrollbar>

            <!-- 底部操作 -->
            <div class="correction-footer">
              <el-button text bg :icon="Plus" @click="addItem" class="add-btn"
                >添加文本块</el-button
              >
              <div class="correction-global-actions">
                <el-button @click="handleClose">取消</el-button>

                <el-button
                  type="primary"
                  :loading="submitting"
                  @click="submitCorrection"
                  >确认提交</el-button
                >
              </div>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted, nextTick } from "vue";
import { ElImageViewer, ElMessage } from "element-plus";
import { Edit, Delete, Plus } from "@element-plus/icons-vue";
import { type OcrResult, ocrResultApi } from "@/api/ocr";
import { ocrResultTypeEnum } from "@/utils/enums";
import { mediaUrl } from "@/api/utils";
import { superRequest } from "@/utils/request";

const props = defineProps<{
  modelValue: boolean;
  result: OcrResult | null;
  targetType: string | number; // 目标修改类型
  taskId: string;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "success", updates: Partial<OcrResult>): void;
}>();

const visible = computed({
  get: () => props.modelValue,
  set: val => emit("update:modelValue", val)
});

const submitting = ref(false);
const correctionTexts = ref<string[]>([]);
const editingIndex = ref<number | null>(null);
const tempEditText = ref("");

// 计算属性
const urlList = computed(() => {
  if (!props.result) return [];
  return [mediaUrl(props.result.image_path)];
});

const panelTitle = computed(() => {
  const type = Number(props.targetType);
  return type === ocrResultTypeEnum.WRONG.value ? "更正文本" : "补全文本";
});

const initCorrection = () => {
  editingIndex.value = null;
  tempEditText.value = "";
  const type = Number(props.targetType);

  if (type === ocrResultTypeEnum.WRONG.value) {
    correctionTexts.value = props.result?.texts ? [...props.result.texts] : [];
    if (correctionTexts.value.length === 0) {
      correctionTexts.value.push("");
      enableEdit(0);
    }
  } else {
    // MISSING or others
    correctionTexts.value = [""];
    enableEdit(0);
  }
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
  if (!props.result) return;

  const validTexts = correctionTexts.value
    .map(t => t.trim())
    .filter(t => t.length > 0);

  if (validTexts.length === 0) {
    ElMessage.warning("内容不能为空");
    return;
  }

  const type = Number(props.targetType);
  if (type === ocrResultTypeEnum.WRONG.value) {
    const currentTexts = props.result.texts || [];
    if (
      validTexts.length === currentTexts.length &&
      validTexts.every((val, index) => val === currentTexts[index])
    ) {
      ElMessage.warning("当前输入的文本与识别结果相同");
      return;
    }
  }

  submitting.value = true;
  try {
    const params: any = {
      id: props.result.id,
      task_id: props.taskId,
      result_type: type,
      corrected_texts: validTexts
    };

    await superRequest({
      apiFunc: ocrResultApi.verify,
      apiParams: params,
      onSucceed: () => {
        emit("success", {
          result_type: type,
          is_verified: true,
          texts: validTexts
        });
        handleClose();
      }
    });
  } catch (error) {
    console.error(error);
  } finally {
    submitting.value = false;
  }
};

const handleClose = () => {
  visible.value = false;
};

watch(
  () => props.modelValue,
  val => {
    if (val && props.result) {
      document.body.classList.add("ocr-verify-mode");
      initCorrection();
    } else {
      document.body.classList.remove("ocr-verify-mode");
    }
  }
);

onUnmounted(() => {
  document.body.classList.remove("ocr-verify-mode");
});
</script>

<style>
/* 复用 OcrResultVerifyer 的全局样式 */
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
.verify-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 3000;
  display: flex;
  justify-content: flex-end;
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

.image-info {
  background: rgba(255, 255, 255, 0.1);
  padding: 12px;
  border-radius: 6px;
  color: #ddd;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.file-path {
  font-size: 12px;
  word-break: break-all;
  opacity: 0.8;
  line-height: 1.4;
}
</style>
