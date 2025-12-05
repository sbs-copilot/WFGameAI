<template>
  <div class="flex-full flex-col">
    <!-- 筛选器 -->
    <el-card class="filter-card" shadow="never">
      <div class="filter-toolbar">
        <!-- 左侧：高频筛选（匹配状态、翻译状态） -->
        <div class="filter-groups-left">
          <div class="filter-group-item">
            <span class="label-text">匹配状态：</span>
            <el-radio-group
              v-model="filterData.has_match"
              @change="fetchResults"
            >
              <el-radio-button
                v-for="item in sortedEnum(ocrIsMatchEnum)"
                :key="item.value"
                :label="item.value"
              >
                {{ item.label }}
              </el-radio-button>
            </el-radio-group>
          </div>
          <div class="filter-group-item">
            <span class="label-text">翻译状态：</span>
            <el-radio-group
              v-model="filterData.is_translated"
              @change="fetchResults"
            >
              <el-radio-button
                v-for="item in sortedEnum(ocrIsTranslatedEnum)"
                :key="item.value"
                :label="item.value"
              >
                {{ item.label }}
              </el-radio-button>
            </el-radio-group>
          </div>
          <div class="filter-group-item">
            <span class="label-text">置信区间：</span>
            <div style="width: 160px; padding: 0 10px">
              <el-slider
                v-model="confidenceRange"
                range
                :min="0"
                :max="1"
                :step="0.01"
                :marks="sliderMarks"
                @change="fetchResults"
              />
            </div>
          </div>
        </div>

        <!-- 右侧：搜索与高级筛选入口 -->
        <div class="filter-actions">
          <el-input
            style="width: 200px"
            v-model="filterData.keyword"
            placeholder="搜索关键字..."
            :prefix-icon="Search"
            clearable
            class="search-input"
            @change="handleSearch"
            @clear="handleSearch"
          />

          <el-button
            :type="hasActiveFilters ? 'primary' : 'default'"
            :plain="!hasActiveFilters"
            @click="toggleExpand"
          >
            <el-icon class="mr-1"><Filter /></el-icon>
            更多筛选
            <el-icon class="el-icon--right">
              <ArrowUp v-if="isExpanded" />
              <ArrowDown v-else />
            </el-icon>
          </el-button>

          <el-tooltip content="重置所有条件" placement="top">
            <el-button :icon="RefreshLeft" circle @click="handleReset" />
          </el-tooltip>

          <el-tooltip content="导出离线报告" placement="top">
            <el-button
              v-if="false"
              :icon="Download"
              circle
              @click="handleExportOfflineReport"
              :loading="exportLoading"
            />
          </el-tooltip>
        </div>
      </div>

      <!-- 展开的高级筛选面板 -->
      <el-collapse-transition>
        <div v-show="isExpanded" class="advanced-filter-panel">
          <div class="filter-groups-advanced">
            <div class="filter-group-item">
              <span class="label-text">审核状态：</span>
              <el-radio-group
                v-model="filterData.is_verified"
                @change="fetchResults"
              >
                <el-radio-button
                  v-for="item in sortedEnum(ocrIsVerifiedEnum)"
                  :key="item.order"
                  :label="item.value"
                >
                  {{ item.label }}
                </el-radio-button>
              </el-radio-group>
            </div>
            <div class="filter-group-item">
              <span class="label-text">结果标记：</span>
              <el-radio-group
                v-model="filterData.result_type"
                @change="fetchResults"
              >
                <el-radio-button
                  v-for="item in sortedEnum(ocrResultTypeEnum)"
                  :key="item.value"
                  :label="item.value"
                >
                  {{ item.label }}
                </el-radio-button>
              </el-radio-group>
            </div>
          </div>
        </div>
      </el-collapse-transition>
    </el-card>

    <!-- 数据卡片 -->
    <el-card
      class="table-card"
      body-style="display: flex; flex-direction: column; flex: 1; overflow: hidden;"
    >
      <div
        class="card-list-container"
        v-loading="loading"
        element-loading-text="正在加载..."
      >
        <el-scrollbar ref="scrollbarRef" @scroll="handleScroll">
          <!-- 图片预览 -->
          <el-image-viewer
            ref="imageViewer"
            v-if="showPreview"
            :url-list="viewerSrcList"
            show-progress
            hide-on-click-modal
            :initial-index="viewerIndex"
            @close="showPreview = false"
          />
          <div v-if="results.length > 0" class="card-grid">
            <el-row :gutter="16">
              <el-col
                class="mb-4"
                v-for="item in results"
                :key="item.id"
                :xs="24"
                :sm="12"
                :md="8"
                :lg="6"
                :xl="4"
              >
                <OcrResultCard
                  :result="item"
                  :task-id="props.taskId"
                  :show-translation="props.showTranslation"
                  @update:result="updateResult(item, $event)"
                  @view-image="handleViewImage"
                  @request-correction="handleRequestCorrection"
                />
              </el-col>
            </el-row>
          </div>
          <el-empty v-else description="暂无数据" />
        </el-scrollbar>
      </div>

      <!-- 分页栏 -->
      <div class="pagination-wrapper">
        <!-- 已审核进度条 -->
        <div class="progress-section" v-if="taskProgress.total > 0">
          <div class="progress-info">
            <span class="label">审核进度</span>
            <span class="value">
              <span class="verified">{{ taskProgress.verified }}</span>
              <span class="divider">/</span>
              <span class="total">{{ taskProgress.total }}</span>
            </span>
          </div>
          <el-progress
            :percentage="progressPercentage"
            :stroke-width="12"
            :show-text="false"
            color="#6425d0"
            class="progress-bar"
          />
          <div class="percentage-text">{{ progressPercentage }}%</div>
          <transition name="el-fade-in">
            <el-button
              v-if="isScrollBottom && unverifiedCorrectItemIds.length > 0"
              type="primary"
              class="ml-4"
              plain
              color="#6425d0"
              round
              size="small"
              @click="handleBatchVerifyPage"
            >
              当前页已审核
            </el-button>
          </transition>
        </div>
        <el-dropdown v-if="false" @command="handleCommand">
          <el-button type="warning" plain style="width: 140px">
            当前页标注为
            <el-icon>
              <ArrowDown class="ml-1" />
            </el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item
                v-for="item in sortedEnum(ocrResultTypeEnum, [
                  ocrResultTypeEnum.ALL
                ])"
                :key="item.value"
                :command="item.value"
              >
                {{ item.label }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-pagination
          :current-page="pagination.currentPage"
          :page-size="pagination.pageSize"
          :total="pagination.total"
          :page-sizes="[30, 60, 120, 240, 480]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handleSizeChange"
          @current-change="handlePageChange"
          background
        />
      </div>
    </el-card>

    <OcrCorrectionDialog
      v-model="correctionVisible"
      :result="currentCorrectionItem"
      :target-type="targetCorrectionType"
      :task-id="props.taskId"
      @success="handleCorrectionSuccess"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch, computed, nextTick } from "vue";
import {
  type OcrResult,
  ocrTaskApi,
  type TaskGetDetailsParams
} from "@/api/ocr";
import {
  Search,
  RefreshLeft,
  ArrowDown,
  ArrowUp,
  Filter,
  Download
} from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { superRequest } from "@/utils/request";
import OcrResultCard from "./OcrResultCard.vue";
import {
  ocrResultTypeEnum,
  ocrIsMatchEnum,
  ocrIsVerifiedEnum,
  ocrIsTranslatedEnum,
  sortedEnum
} from "@/utils/enums";
import { mediaUrl } from "@/api/utils";
import { ocrResultApi } from "@/api/ocr";
import OcrCorrectionDialog from "./OcrCorrectionDialog.vue";
import { message } from "@/utils/message";

const props = defineProps<{
  taskId: string;
  showTranslation?: boolean;
}>();

const loading = ref(false);
const exportLoading = ref(false);
const results = ref<OcrResult[]>([]);
const isScrollBottom = ref(false);
const filterData = reactive<Partial<TaskGetDetailsParams>>({
  has_match: null,
  result_type: "",
  keyword: "",
  is_verified: null,
  is_translated: null
});
const confidenceRange = ref([0, 1]);
const sliderMarks = computed(() => {
  const [min, max] = confidenceRange.value;
  // 避免两个值太近导致重叠，这里简单处理，实际可能需要更复杂的逻辑
  // 或者直接返回两个值，Element Plus 会处理位置
  return {
    [min]: min.toFixed(2),
    [max]: max.toFixed(2)
  };
});

const pagination = reactive({
  currentPage: 1,
  pageSize: 30,
  total: 0
});

const showPreview = ref(false);
const imageViewer = ref(null);
const scrollbarRef = ref(null);
const viewerIndex = ref(0);
const viewerSrcList = computed(() => {
  const list: string[] = [];
  results.value.forEach(item => {
    list.push(mediaUrl(item.image_path));
    if (props.showTranslation && item.is_translated && item.trans_image_path) {
      list.push(mediaUrl(item.trans_image_path));
    }
  });
  return list;
});

const correctionVisible = ref(false);
const currentCorrectionItem = ref<OcrResult | null>(null);
const targetCorrectionType = ref(0);

const taskProgress = reactive({
  total: 0,
  verified: 0
});

const progressPercentage = computed(() => {
  if (taskProgress.total <= 0) return 0;
  const p = Math.round((taskProgress.verified / taskProgress.total) * 100);
  return p > 100 ? 100 : p;
});

const fetchTaskProgress = () => {
  if (!props.taskId) return;
  superRequest({
    apiFunc: ocrTaskApi.get,
    apiParams: props.taskId,
    onSucceed: data => {
      taskProgress.total = data.total_images || 0;
      taskProgress.verified = data.verified_images || 0;
    }
  });
};

// 滚动到顶部
const scrollToTop = () => {
  if (scrollbarRef.value) {
    scrollbarRef.value.setScrollTop(0);
  }
};

const updateResult = (item: OcrResult, updates: Partial<OcrResult>) => {
  Object.assign(item, updates);
  fetchTaskProgress();
};

const handleRequestCorrection = (item: OcrResult, targetType: number) => {
  currentCorrectionItem.value = item;
  targetCorrectionType.value = targetType;
  correctionVisible.value = true;
};

const handleCorrectionSuccess = (updates: Partial<OcrResult>) => {
  if (currentCorrectionItem.value) {
    updateResult(currentCorrectionItem.value, updates);
  }
};

const handleViewImage = (result: OcrResult, type = "original") => {
  let index = 0;
  for (const item of results.value) {
    if (item.id === result.id) {
      if (
        type === "translated" &&
        props.showTranslation &&
        item.is_translated
      ) {
        index++; // The translated image is after the original one
      }
      viewerIndex.value = index;
      showPreview.value = true;
      return;
    }
    index++;
    if (props.showTranslation && item.is_translated && item.trans_image_path) {
      index++;
    }
  }
};

const fetchResults = async () => {
  if (!props.taskId) return;
  loading.value = true;
  try {
    const params: TaskGetDetailsParams = {
      id: props.taskId,
      page: pagination.currentPage,
      page_size: pagination.pageSize,
      ...filterData,
      min_confidence: confidenceRange.value[0],
      max_confidence: confidenceRange.value[1]
    };
    const res = await superRequest({
      apiFunc: ocrTaskApi.getDetails,
      apiParams: params
    });
    results.value = res?.data?.results || [];
    pagination.total = res?.data?.total || 0;
    await nextTick();
    scrollToTop();

    if (scrollbarRef.value?.wrapRef) {
      const { clientHeight, scrollHeight, scrollTop } =
        scrollbarRef.value.wrapRef;
      isScrollBottom.value = scrollHeight - scrollTop - clientHeight < 50;
    } else {
      isScrollBottom.value = false;
    }
  } catch (error) {
    ElMessage.error("获取结果列表失败");
    console.error(error);
  } finally {
    loading.value = false;
  }
};

const handleSearch = () => {
  pagination.currentPage = 1;
  fetchResults();
};

const handleReset = () => {
  filterData.has_match = null;
  filterData.result_type = "";
  filterData.keyword = "";
  filterData.is_verified = null;
  filterData.is_translated = null;
  confidenceRange.value = [0, 1];
  handleSearch();
};

const handleSizeChange = async (size: number) => {
  pagination.pageSize = size;
  pagination.currentPage = 1; // 回到第一页
  await fetchResults();
};

const handlePageChange = async (page: number) => {
  pagination.currentPage = page;
  await fetchResults();
};

const handleCommand = async (command: string) => {
  const resultType = String(command);
  try {
    await superRequest({
      apiFunc: ocrResultApi.update,
      apiParams: {
        ids: results.value.reduce((acc, item) => {
          acc[item.id] = resultType;
          return acc;
        }, {} as Record<string, string>)
      },
      enableSucceedMsg: true,
      succeedMsgContent: "批量标准成功"
    });
  } finally {
    fetchResults();
  }
};

// 当前页面未审核的正确项
const unverifiedCorrectItemIds = computed(() =>
  results.value
    .filter(
      item =>
        item.result_type === ocrResultTypeEnum.RIGHT.value && !item.is_verified
    )
    .map(item => item.id)
);

const handleScroll = ({ scrollTop }: { scrollTop: number }) => {
  const wrap = scrollbarRef.value?.wrapRef;
  if (wrap) {
    const { clientHeight, scrollHeight } = wrap;
    isScrollBottom.value = scrollHeight - scrollTop - clientHeight < 50;
  }
};

const handleBatchVerifyPage = async () => {
  const data = {
    task_id: props.taskId,
    ids: unverifiedCorrectItemIds.value || []
  };

  if (data.ids.length === 0) {
    message("当前页没有需要标记为已审核的项", { type: "info" });
    return;
  }

  await superRequest({
    apiFunc: ocrResultApi.batchVerifyRight,
    apiParams: data
  });

  fetchTaskProgress();

  let messageText = "本页标记为已审核成功，已为您加载下一页数据";
  if (pagination.currentPage * pagination.pageSize < pagination.total) {
    pagination.currentPage++;
  } else {
    messageText = "本页标记为已审核成功，当前已是最后一页";
  }
  await fetchResults();
  message(messageText, { type: "success" });
};

const handleExportOfflineReport = async () => {
  if (!props.taskId) return;
  exportLoading.value = true;
  try {
    const res = await superRequest({
      apiFunc: ocrTaskApi.exportOfflineHtml,
      apiParams: props.taskId
    });

    if (res && res.data && res.data.url) {
      const url = mediaUrl(res.data.url);
      ElMessage.success("离线报告生成任务已提交，请稍后下载");

      // 简单的延迟下载尝试，或者提示用户
      setTimeout(async () => {
        try {
          const response = await fetch(url);
          const blob = await response.blob();
          const link = document.createElement("a");
          link.href = URL.createObjectURL(blob);
          link.setAttribute("download", url.split("/").pop() || "report.html");
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          URL.revokeObjectURL(link.href);
        } catch (e) {
          console.error(e);
          window.open(url, "_blank");
        }
      }, 2000);
    }
  } catch (error) {
    console.error(error);
    ElMessage.error("导出请求失败");
  } finally {
    exportLoading.value = false;
  }
};

const hasActiveFilters = computed(() => {
  return !!filterData.result_type || filterData.is_verified !== null;
});

const isExpanded = ref(false);
const toggleExpand = () => {
  isExpanded.value = !isExpanded.value;
};

watch(
  () => props.taskId,
  () => {
    fetchResults();
    fetchTaskProgress();
  },
  { immediate: true }
);
</script>

<style lang="scss" scoped>
.filter-card {
  margin-bottom: 16px;
  flex-shrink: 0;

  :deep(.el-card__body) {
    padding: 16px 24px;
  }
}

.filter-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
}

.filter-groups-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 24px;
}

.filter-group-item {
  display: flex;
  align-items: center;

  .label-text {
    font-size: 14px;
    color: #606266;
    margin-right: 6px;
    font-weight: 500;
  }
}
.filter-actions {
  display: flex;
  align-items: center;
  gap: 12px;

  .search-input {
    width: 240px;
  }
}

.advanced-filter-panel {
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid #f0f2f5;
}

.filter-groups-advanced {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 24px;
}

.table-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.card-list-container {
  flex: 1;
  overflow: hidden;
}

.card-grid {
  padding: 8px;
}

.pagination-wrapper {
  padding-top: 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
}

.progress-section {
  display: flex;
  align-items: center;
  background-color: #f8f9fb;
  padding: 8px 20px;
  border-radius: 24px;
  margin-right: 16px;
  border: 1px solid #ebeef5;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.04);
  transition: all 0.3s ease;

  &:hover {
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
    transform: translateY(-1px);
  }

  .progress-info {
    display: flex;
    align-items: center;
    margin-right: 20px;
    font-size: 15px;

    .label {
      color: #606266;
      margin-right: 10px;
    }

    .value {
      font-weight: 600;

      .verified {
        color: #6425d0;
      }

      .divider {
        margin: 0 4px;
        color: #c0c4cc;
        font-weight: normal;
      }

      .total {
        color: #909399;
      }
    }
  }

  .progress-bar {
    width: 150px;
    margin-right: 12px;
  }

  .percentage-text {
    font-size: 15px;
    color: #6425d0;
    font-weight: 600;
    min-width: 40px;
    text-align: right;
  }
}
</style>
