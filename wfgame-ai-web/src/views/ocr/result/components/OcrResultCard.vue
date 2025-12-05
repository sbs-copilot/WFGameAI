<template>
  <el-card shadow="always" class="result-card">
    <div class="card-content cursor-pointer">
      <div
        class="image-container"
        :class="{
          'flex flex-col': showTransImage
        }"
      >
        <div class="image-wrapper">
          <el-image
            :title="mediaUrl(result.image_path)"
            :style="{
              width: '100%',
              height: showTransImage ? '180px' : '180px'
            }"
            :src="mediaUrl(result.image_path)"
            fit="scale-down"
            lazy
            @click="handleImageClick(result, 'original')"
          />
          <div v-if="showTransImage" class="image-label original">原图</div>
        </div>
        <div v-if="showTransImage" class="image-wrapper">
          <el-image
            :title="
              result.trans_image_path
                ? mediaUrl(result.trans_image_path)
                : '无翻译图'
            "
            style="width: 100%; height: 180px"
            :src="
              result.trans_image_path ? mediaUrl(result.trans_image_path) : ''
            "
            fit="scale-down"
            lazy
            @click="handleImageClick(result, 'translated')"
          >
            <template #error>
              <div
                class="flex flex-col items-center justify-center h-full text-gray-300 w-full"
                style="height: 180px"
              >
                <el-icon size="50"><Picture /></el-icon>
                <span class="mt-4 text-sm">无翻译图</span>
              </div>
            </template>
          </el-image>
          <div class="image-label translated">翻译</div>
        </div>
      </div>
      <div class="info-container">
        <h3 class="image-name" :title="getImgName(result.image_path)">
          {{ getImgName(result.image_path) }}
        </h3>
        <p class="info-item">
          <span :title="`ID:${result.id}`">机器识别:</span>
          <span
            :title="getResultText(result)"
            @click="handleCopyText(getResultText(result))"
            :class="{
              'text-primary font-semibold':
                result.result_type == ocrResultTypeEnum.RIGHT.value,
              'line-through text-red-300':
                result.result_type == ocrResultTypeEnum.WRONG.value
            }"
          >
            {{ getResultText(result) || "-" }}
          </span>
        </p>
        <p
          v-if="result.result_type == ocrResultTypeEnum.WRONG.value"
          class="info-item"
        >
          <span>人工矫正:</span>
          <span
            class="text-primary font-semibold"
            :title="getResultCorrectedText(result)"
            @click="handleCopyText(getResultCorrectedText(result))"
          >
            {{ getResultCorrectedText(result) || "（空）" }}
          </span>
        </p>
        <p class="info-item">
          最大置信度:
          <span>
            {{ result.max_confidence || "-" }}
          </span>
        </p>
        <p class="info-item">
          分辨率:
          <span>
            {{ result.pic_resolution || "-" }}
          </span>
        </p>
        <p v-if="false" class="info-item">
          语言:
          <span>
            {{ result.languages || "-" }}
          </span>
        </p>
        <p v-if="false" class="info-item" :title="result.image_path">
          路径:
          <a :href="mediaUrl(result.image_path)" target="_blank">
            {{ result.image_path || "-" }}
          </a>
        </p>
        <p v-if="false" class="info-item" :title="result.image_path">
          哈希值:
          <span
            :title="result.image_hash"
            @click="handleCopyText(result.image_hash)"
          >
            {{ result.image_hash || "-" }}
          </span>
        </p>
        <el-radio-group
          :model-value="props.result.result_type"
          size="small"
          class="result-type-radios"
          @update:model-value="handleResultTypeChange"
        >
          <el-radio-button
            v-for="item in resultTypes"
            :key="item.value"
            :label="item.value"
            :style="{
              '--radio-bg-color': item.color,
              '--radio-text-color': '#303133'
            }"
          >
            {{ item.label }}
          </el-radio-button>
        </el-radio-group>
        <VerifiedSVG
          v-if="result.is_verified"
          class="is_verified"
          title="已人工审核"
        />
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { type OcrResult, ocrResultApi } from "@/api/ocr";
import { ocrResultTypeEnum } from "@/utils/enums";
import { ref, computed } from "vue";
import { superRequest } from "@/utils/request";
import { mediaUrl } from "@/api/utils";
import { copyText } from "@/utils/utils";
import { Picture } from "@element-plus/icons-vue";

import VerifiedSVG from "@/assets/svg/verified.svg?component";

type Emits = {
  (e: "view-image", result: OcrResult, type: string): void;
  (e: "update:result", updates: Partial<OcrResult>): void;
  (e: "request-correction", result: OcrResult, targetType: number): void;
};

const emit = defineEmits<Emits>();

const props = defineProps<{
  result: OcrResult;
  taskId: string;
  showTranslation?: boolean;
}>();

const showTransImage = computed(() => {
  return props.showTranslation && props.result.is_translated;
});

const currentResultType = ref(props.result.result_type);

const resultTypes = computed(() => {
  return Object.values(ocrResultTypeEnum).filter(item => item.value > 0);
});

const getResultText = (result: OcrResult) => {
  if (!result.texts) return "";
  if (Array.isArray(result.texts)) {
    const texts = result.texts.map(s => s).join("");
    return texts;
  }
  return result.texts;
};

const getResultCorrectedText = (result: OcrResult) => {
  if (!result.corrected_texts) return "";
  if (Array.isArray(result.corrected_texts)) {
    const texts = result.corrected_texts.map(s => s).join("");
    return texts;
  }
  return result.corrected_texts;
};

const handleCopyText = (text: string) => {
  if (!text) return;
  copyText(text);
};

const handleImageClick = (result: OcrResult, type = "original") => {
  emit("view-image", result, type);
};

const getImgName = (imagePath: string) => {
  if (!imagePath) return "未知图片";
  const parts = imagePath.split("/");
  return parts[parts.length - 1];
};

const submitUpdate = (newType: number) => {
  const oldValues = {
    result_type: currentResultType.value,
    is_verified: props.result.is_verified
  };

  const updates: Partial<OcrResult> = {
    result_type: newType,
    is_verified: true
  };

  // 先发送 emit 事件给父组件
  emit("update:result", updates);

  const params: any = {
    id: props.result.id,
    task_id: props.taskId,
    result_type: newType
  };

  // 然后调用 API 更新
  superRequest({
    apiFunc: ocrResultApi.verify,
    apiParams: params,
    onSucceed: () => {
      currentResultType.value = newType;
    },
    onFailed: () => {
      // API 调用失败时，发送原来的值给父组件（回滚）
      emit("update:result", oldValues);
    }
  });
};

const handleResultTypeChange = async (newType: number) => {
  const oldType = props.result.result_type;
  if (newType === oldType) return;

  // 当更新为非正确时，必须传递 corrected_texts，请求父组件打开矫正弹窗
  if (newType == ocrResultTypeEnum.WRONG.value) {
    emit("request-correction", props.result, newType);
    return;
  }

  // 正确类型，直接提交更新
  submitUpdate(newType);
};
</script>

<style lang="scss" scoped>
.result-card {
  height: 100%;
  display: flex;
  flex-direction: column;
  transition: background-color 0.3s;

  :deep(.el-card__body) {
    padding: 0;
    flex: 1;
  }
}

.card-content {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.image-wrapper {
  position: relative;
  height: 100%;
}

.image-label {
  position: absolute;
  top: 6px;
  right: 6px;
  font-size: 12px;
  line-height: 1.2;
  padding: 4px 8px;
  border-radius: 4px;
  z-index: 1;
  backdrop-filter: blur(4px);
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  font-weight: 500;
  transition: all 0.3s ease;

  &.original {
    background-color: rgba(255, 255, 255, 0.85);
    color: #606266;
  }

  &.translated {
    background-color: rgba(236, 245, 255, 0.9);
    color: #81c0ff;
  }
}

.image-container {
  flex-shrink: 0;
  padding: 6px;
  border-bottom: 1px solid #e4e7ed;
}

.info-container {
  padding: 14px;
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
  z-index: 0;

  .is_verified {
    position: absolute;
    top: 46px;
    right: 4px;
    width: 80px;
    height: 80px;
    opacity: 0.2;
    z-index: -1;
  }
}

.image-name {
  font-size: 16px;
  font-weight: bold;
  margin: 0 0 10px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.info-item {
  font-size: 12px;
  margin: 2px 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: grey;
}

.result-type-radios {
  margin-top: auto;
  padding-top: 10px;
  margin-left: auto;
  margin-right: auto;
  :deep(.el-radio-button__inner) {
    background-color: #fff;
    color: #303133;
    border-color: #dcdfe6;
    box-shadow: none;
  }

  :deep(.el-radio-button.is-active .el-radio-button__inner) {
    background-color: var(--radio-bg-color, #409eff);
    border-color: var(--radio-bg-color, #409eff);
    color: #fff;
    box-shadow: -1px 0 0 0 var(--radio-bg-color, #409eff);
  }

  :deep(.el-radio-button:first-child .el-radio-button__inner) {
    border-left: 1px solid #dcdfe6;
  }
}
</style>
