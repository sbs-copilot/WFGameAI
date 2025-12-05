<template>
  <div class="flex-full flex-col gap-2">
    <!-- 筛选器 -->
    <el-card class="filter-card">
      <el-form :model="filterData" inline>
        <el-form-item label="状态">
          <el-select
            v-model="filterData.status"
            placeholder="所有状态"
            clearable
            style="width: 120px"
            @change="loadData"
          >
            <el-option
              v-for="item in sortedEnum(taskStatusEnum)"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="时间范围">
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            style="width: 240px"
            @change="handleDateChange"
          />
        </el-form-item>
        <el-form-item>
          <el-button :icon="Search" type="primary" @click="handleSearch" plain>
            搜索
          </el-button>
          <el-button :icon="RefreshLeft" @click="handleReset"> 重置 </el-button>
        </el-form-item>
      </el-form>
    </el-card>
    <!-- 数据表 -->
    <el-card class="table-card">
      <!-- 表格 -->
      <el-table
        :data="tableData"
        v-loading="loading"
        stripe
        style="width: 100%"
        height="100%"
      >
        <el-table-column prop="id" label="任务ID" width="220">
          <template #default="{ row }">
            <el-link type="primary" @click="handleViewTask(row)">
              {{ row.id }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="数据来源" width="100">
          <template #default="{ row }">
            <div class="source-info">
              <el-popover
                placement="top"
                trigger="hover"
                width="300"
                :content="getSourceDisplay(row)"
              >
                <template #reference>
                  <el-tag
                    :type="row.source_type === 'git' ? 'success' : 'warning'"
                    size="small"
                    effect="plain"
                    class="source-type-tag"
                  >
                    {{ row.source_type === "git" ? "Git" : "Upload" }}
                  </el-tag>
                </template>
              </el-popover>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="任务状态" width="120">
          <template #default="{ row }">
            <el-tag
              style="width: 80px; padding: 4px"
              :type="getEnumEntry(taskStatusEnum, row.status)?.type || 'info'"
              effect="dark"
              round
            >
              {{ getLabel(taskStatusEnum, row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="progress" label="执行进度" width="180">
          <template #default="{ row }">
            <div>
              <el-progress
                v-if="row.status === taskStatusEnum.RUNNING.value"
                text-inside
                :percentage="
                  parseInt((row.processed_images / row.total_images) * 100) || 0
                "
                :stroke-width="25"
                striped
                striped-flow
                :duration="15"
              />
              <el-progress
                v-else-if="row.status === taskStatusEnum.COMPLETED.value"
                text-inside
                :percentage="100"
                status="success"
                :stroke-width="25"
                striped
              />
              <el-progress
                v-else-if="row.status === taskStatusEnum.FAILED.value"
                text-inside
                :percentage="100"
                status="exception"
                :stroke-width="25"
                striped
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="remark" label="进度说明">
          <template #default="{ row }">
            <el-text
              :title="row.remark"
              :type="getEnumEntry(taskStatusEnum, row.status)?.type"
              style="
                font-size: 14px;
                line-height: normal;
                white-space: pre-line;
              "
            >
              {{ row.remark || "-" }}
            </el-text>
          </template>
        </el-table-column>
        <el-table-column prop="total_images" label="总图片数" width="100" />
        <el-table-column prop="matched_images" label="总命中数" width="100" />
        <el-table-column
          prop="match_rate"
          label="匹配率"
          width="100"
          align="center"
        >
          <template #default="{ row }">
            <span>{{ `${row.match_rate}%` }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="duration" label="耗时" width="120">
          <template #default="{ row }">
            {{ getDurationDisplay(row) }}
          </template>
        </el-table-column>
        <el-table-column
          prop="results_count"
          label="结果数"
          width="100"
          align="center"
          v-if="false"
        >
          <template #default="{ row }">
            <el-tag v-if="row.results_count > 0" type="success" size="small">
              {{ row.results_count }}
            </el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column
          prop="audit_progress"
          label="审核情况"
          width="100"
          align="center"
        >
          <template #default="{ row }">
            <div class="flex flex-col justify-center items-center">
              <span class="text-xs mb-1">{{
                `${row.verified_images} / ${row.total_images}`
              }}</span>
              <el-progress
                :percentage="
                  row.total_images
                    ? Math.min(
                        100,
                        Math.round(
                          (row.verified_images / row.total_images) * 100
                        )
                      )
                    : 0
                "
                :show-text="false"
                :stroke-width="6"
                status="success"
                style="width: 100%"
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建信息" width="150">
          <template #default="{ row }">
            <div class="creation-info">
              <span class="creator">{{ row.creator_name || "未知" }}</span>
              <span class="create-time">{{ formatDate(row.created_at) }}</span>
            </div>
          </template>
        </el-table-column>

        <!-- 操作列 -->
        <el-table-column label="操作" fixed="right" :width="280">
          <template #default="{ row }">
            <el-button
              v-if="isFinishedTask(row)"
              type="primary"
              size="small"
              plain
              :icon="ZoomIn"
              @click="handleViewResults(row)"
            >
              查看
            </el-button>
            <el-button
              v-if="isFinishedTask(row)"
              type="warning"
              size="small"
              plain
              :icon="Download"
              @click="handleDownload(row)"
            >
              下载
            </el-button>
            <el-popconfirm title="是否确认删除?" @confirm="handleDelete(row)">
              <template #reference>
                <el-button type="danger" size="small" plain :icon="Delete">
                  删除
                </el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页栏 -->
      <div class="pagination-wrapper">
        <el-pagination
          :current-page="pagination.currentPage"
          :page-size="pagination.pageSize"
          :total="pagination.total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handleSizeChange"
          @current-change="handlePageChange"
        />
      </div>

      <!-- 空状态 -->
      <div
        v-if="!loading && tableData.length === 0 && false"
        class="empty-state"
      >
        <el-empty description="暂无任务记录">
          <el-button type="primary" @click="handleCreateTask">
            创建新任务
          </el-button>
        </el-empty>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted } from "vue";
import {
  Search,
  RefreshLeft,
  Delete,
  ZoomIn,
  Download
} from "@element-plus/icons-vue";
import type { OcrTask } from "@/api/ocr";
import type { OcrFilter } from "../utils/types";
import {
  taskStatusEnum,
  sortedEnum,
  getLabel,
  getEnumEntry
} from "@/utils/enums";
import { copyText } from "@/utils/utils";
import { useNavigate } from "@/views/common/utils/navHook";
import { useOcr } from "@/views/ocr/list/utils/hook";
import { useSSE, SSEEvent } from "@/layout/components/sseState/useSSE";
import { debounce } from "@/utils/utils";

const { on } = useSSE();

const currentTime = ref(new Date());
let timerInterval: number | null = null;

onMounted(() => {
  timerInterval = window.setInterval(() => {
    currentTime.value = new Date();
  }, 1000);
});

onUnmounted(() => {
  if (timerInterval) clearInterval(timerInterval);
});

const formatDuration = (ms: number) => {
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
};

const getDurationDisplay = (row: OcrTask) => {
  if (row.status === taskStatusEnum.PENDING.value) {
    return "00:00:00";
  }
  if (row.status === taskStatusEnum.RUNNING.value) {
    if (!row.start_time) return "00:00:00";
    const start = new Date(row.start_time).getTime();
    const now = currentTime.value.getTime();
    const diff = Math.max(0, now - start);
    return formatDuration(diff);
  }
  return row.duration || "-";
};

const updateTask = (updatedTask: OcrTask) => {
  const index = tableData.value.findIndex(task => task.id === updatedTask.id);
  if (index !== -1) {
    tableData.value[index] = { ...tableData.value[index], ...updatedTask };
  }
};
// 监听 OCR 任务更新事件
on(SSEEvent.OCR_TASK_UPDATE, (data: OcrTask) => {
  debounce(updateTask, 100)(data);
});

const { navigateToOcrResult } = useNavigate();
const { deleteTask, downloadTask } = useOcr();

interface Props {
  fetchData: (params: any) => Promise<any>;
}

interface Emits {
  (e: "view-task", task: OcrTask): void;
  (e: "view-results", task: OcrTask): void;
  (e: "download", task: OcrTask): void;
  (e: "delete", task: OcrTask): void;
  (e: "create-task"): void;
  (e: "data-loaded", data: { list: OcrTask[]; total: number }): void;
}

const props = defineProps<Props>();
const emit = defineEmits<Emits>();

const loading = ref(false);
const tableData = ref<OcrTask[]>([]);
const dateRange = ref<[Date, Date] | null>(null);

const initialFilterData: OcrFilter = {
  startDate: "",
  endDate: "",
  status: ""
};

const filterData = reactive<OcrFilter>({ ...initialFilterData });

const pagination = reactive({
  currentPage: 1,
  pageSize: 20,
  total: 0
});

const isFinishedTask = (task: OcrTask) => {
  return (
    task.status === taskStatusEnum.COMPLETED.value ||
    task.status === taskStatusEnum.FAILED.value
  );
};

const loadData = async () => {
  loading.value = false;
  try {
    const params = {
      ...filterData,
      page: pagination.currentPage,
      page_size: pagination.pageSize
    };
    const res = await props.fetchData(params);
    tableData.value = res.data.tasks || [];
    pagination.total = res.data.total || 0;
    emit("data-loaded", res);
  } catch (error) {
    console.error("Failed to fetch table data:", error);
    tableData.value = [];
    pagination.total = 0;
  } finally {
    loading.value = false;
  }
};

const handleSearch = () => {
  pagination.currentPage = 1;
  loadData();
};

const handleReset = () => {
  Object.assign(filterData, initialFilterData);
  dateRange.value = null;
  pagination.currentPage = 1;
  loadData();
};

const handlePageChange = (page: number) => {
  pagination.currentPage = page;
  loadData();
};

const handleSizeChange = (size: number) => {
  pagination.pageSize = size;
  pagination.currentPage = 1;
  loadData();
};

const getSourceDisplay = (task: OcrTask) => {
  if (task.source_type === "git") {
    return `${task.git_repository_url} (${task.git_branch})`;
  } else {
    return "用户自定义上传";
  }
};

const formatDate = (dateString: string) => {
  if (!dateString) return "-";
  const date = new Date(dateString);
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
};

const handleDateChange = (dates: [Date, Date] | null) => {
  if (dates) {
    filterData.startDate = dates[0].toISOString().split("T")[0];
    filterData.endDate = dates[1].toISOString().split("T")[0];
  } else {
    filterData.startDate = "";
    filterData.endDate = "";
  }
  loadData();
};

const handleViewTask = (task: OcrTask) => {
  copyText(task.id);
  emit("view-task", task);
};

const handleViewResults = (task: OcrTask) => {
  emit("view-results", task);
  navigateToOcrResult(task.id);
};

const handleDownload = (task: OcrTask) => {
  downloadTask(task);
  emit("download", task);
};

const handleDelete = (task: OcrTask) => {
  deleteTask(task.id).then(() => {
    loadData();
    emit("delete", task);
  });
};

const handleCreateTask = () => {
  emit("create-task");
};

const refresh = () => {
  loadData();
};

defineExpose({
  refresh
});
</script>

<style scoped>
.filter-card {
  border-radius: 8px;
  height: auto;
}

.table-card {
  border-radius: 8px;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.pagination-wrapper {
  display: flex;
  justify-content: center;
  margin-top: 20px;
}
.empty-state {
  padding: 40px 20px;
  text-align: center;
  margin-top: 20px;
}
.source-info {
  display: flex;
  align-items: center;
  gap: 8px;
}
.source-type-tag {
  flex-shrink: 0;
  cursor: pointer;
}
.creation-info {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}
.creator {
  font-weight: 500;
}
.create-time {
  font-size: 12px;
  color: #909399;
}
.el-progress--line {
  display: flex;
  align-items: center;
}

.el-progress--line .el-progress__text {
  font-size: 12px !important;
  color: #606266;
  margin-left: 8px;
}
</style>
