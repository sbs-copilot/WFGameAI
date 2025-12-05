<template>
  <el-dialog
    v-model="visible"
    title="🧐人工矫正"
    width="900px"
    append-to-body
    destroy-on-close
    :close-on-click-modal="false"
    align-center
    @closed="handleClosed"
  >
    <div class="correction-dialog-body" v-if="result">
      <div class="dialog-left">
        <el-image
          :src="mediaUrl(result.image_path)"
          fit="contain"
          class="dialog-image"
          hide-on-click-modal
          :preview-src-list="[mediaUrl(result.image_path)]"
        >
          <template #error>
            <div class="image-slot">
              <el-icon><icon-picture /></el-icon>
            </div>
          </template>
        </el-image>
      </div>
      <div class="dialog-right">
        <div class="current-text-section">
          <div class="section-header">
            <div class="section-title">当前识别结果</div>
            <el-tag size="small" type="info">参考</el-tag>
          </div>
          <div class="text-content">
            {{ getResultText(result) || "无文本" }}
          </div>
        </div>
        <div class="correction-input-section">
          <div class="section-header">
            <div class="section-title">人工矫正文本</div>
            <el-button
              type="primary"
              link
              size="small"
              @click="correctionText = ''"
            >
              清空
            </el-button>
          </div>
          <el-input
            v-model="correctionText"
            type="textarea"
            :rows="8"
            placeholder="请输入正确的文本"
            resize="none"
            class="correction-textarea"
          />
        </div>
      </div>
    </div>
    <template #footer>
      <span class="dialog-footer">
        <el-button @click="handleDialogCancel">取消</el-button>
        <el-tooltip
          :content="submitDisabledReason"
          :disabled="canSubmit"
          placement="top"
        >
          <span style="margin-left: 12px">
            <el-button
              type="primary"
              :loading="submitting"
              :disabled="!canSubmit"
              @click="handleDialogConfirm"
            >
              确定
            </el-button>
          </span>
        </el-tooltip>
      </span>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch, computed } from "vue";
import { type OcrResult, ocrResultApi } from "@/api/ocr";
import { mediaUrl } from "@/api/utils";
import { superRequest } from "@/utils/request";
import { Picture as IconPicture } from "@element-plus/icons-vue";

const props = defineProps<{
  modelValue: boolean;
  result: OcrResult | null;
  targetType: number; // 目标修改类型
  taskId: string;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", visible: boolean): void;
  (e: "success", updates: Partial<OcrResult>): void;
}>();

const visible = computed({
  get: () => props.modelValue,
  set: val => emit("update:modelValue", val)
});

const correctionText = ref("");
const submitting = ref(false);

const getResultText = (result: OcrResult) => {
  if (!result || !result.texts) return "";
  if (Array.isArray(result.texts)) {
    return result.texts.join("");
  }
  return result.texts;
};

const canSubmit = computed(() => {
  if (!props.result) return false;
  const originalText = getResultText(props.result);
  const newText = correctionText.value;
  return newText !== originalText;
});

const submitDisabledReason = computed(() => {
  if (!props.result) return "";
  const originalText = getResultText(props.result);
  const newText = correctionText.value;
  if (!newText || newText.trim() === "") {
    return "校正文本不能为空";
  }
  if (newText === originalText) {
    return "校正文本不能与原始结果相同";
  }
  return "";
});

// 初始化数据
watch(
  () => props.modelValue,
  val => {
    if (val && props.result) {
      // 默认填入当前识别结果作为参考
      correctionText.value = getResultText(props.result);
    }
  }
);

const handleClosed = () => {
  correctionText.value = "";
  submitting.value = false;
};

const handleDialogCancel = () => {
  visible.value = false;
};

const handleDialogConfirm = async () => {
  if (!props.result) return;

  const newType = props.targetType;
  const correctedTexts = [correctionText.value];

  submitting.value = true;

  try {
    const params: any = {
      id: props.result.id,
      task_id: props.taskId,
      result_type: newType,
      corrected_texts: correctedTexts
    };

    await superRequest({
      apiFunc: ocrResultApi.verify,
      apiParams: params,
      onSucceed: () => {
        // 成功后通知父组件更新数据
        emit("success", {
          result_type: newType,
          is_verified: true,
          corrected_texts: correctedTexts
        });
        visible.value = false;
      }
    });
  } catch (error) {
    console.error(error);
  } finally {
    submitting.value = false;
  }
};
</script>

<style lang="scss" scoped>
.correction-dialog-body {
  display: flex;
  gap: 24px;
  height: 60vh;
  max-height: 600px;
  min-height: 400px;
}

.dialog-left {
  height: 100%;
  aspect-ratio: 9 / 16;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: #f5f7fa;
  flex-shrink: 0;
}

.dialog-image {
  width: 100%;
  height: 100%;
  display: block;
}

.image-slot {
  display: flex;
  justify-content: center;
  align-items: center;
  width: 100%;
  height: 100%;
  background: #f5f7fa;
  color: #909399;
  font-size: 30px;
}

.dialog-right {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 20px;
  min-width: 0;
}

.current-text-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background-color: #fff;
}

.correction-input-section {
  flex: 1.2;
  display: flex;
  flex-direction: column;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.section-title {
  font-weight: 600;
  font-size: 15px;
  color: #303133;
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-title::before {
  content: "";
  display: block;
  width: 4px;
  height: 16px;
  background-color: #409eff;
  border-radius: 2px;
}

.text-content {
  flex: 1;
  overflow-y: auto;
  font-size: 14px;
  line-height: 1.6;
  color: #606266;
  word-break: break-all;
  padding: 12px;
  background-color: #f5f7fa;
  border-radius: 4px;
  border: 1px solid #e4e7ed;
}

.correction-textarea {
  flex: 1;
  :deep(.el-textarea__inner) {
    height: 100%;
    font-family: inherit;
  }
}
</style>
