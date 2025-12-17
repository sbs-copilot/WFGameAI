<script setup lang="ts">
import { ref, computed } from "vue";
import {
  Search,
  Refresh,
  Connection,
  Document,
  Monitor,
  Grid,
  List,
  Lock,
  Unlock,
  Message,
  Edit,
  Check,
  Close
} from "@element-plus/icons-vue";
import type { DeviceItem, DeviceStats } from "@/api/devices";
// import DeviceReportDialog from "./deviceReportDialog.vue";
// import UsbCheckDialog from "./usbCheckDialog.vue";
import { getEnumEntry, deviceStatusEnum } from "@/utils/enums";
import { TimeDefault } from "@/utils/time";
import { useUserStore } from "@/store/modules/user";
// import { hasAuth } from "@/router/utils";
const userStore = useUserStore();

const loginedUsername = computed(() => userStore.username || "");

defineOptions({
  name: "DevicesTable"
});

const props = defineProps<{
  devices: DeviceItem[];
  loading: boolean;
  error: string;
  stats: DeviceStats;
  searchQuery: string;
  statusFilter: string;
  viewMode: string;
  filteredSortedDevices: DeviceItem[];
}>();

const emit = defineEmits([
  "connect",
  "generate-report",
  "refresh",
  "reserve",
  "release",
  "update:search-query",
  "update:status-filter",
  "update:view-mode",
  "view-log",
  "remind",
  "update-device-name"
]);

const reportDialogRef = ref();
const usbDialogRef = ref();
const sortField = ref("device_id");
const sortDirection = ref("asc");

// 名称编辑状态管理
const editingDeviceId = ref<string | number | null>(null);
const editingName = ref("");

// 排序处理
const sortBy = (field: string) => {
  if (sortField.value === field) {
    sortDirection.value = sortDirection.value === "asc" ? "desc" : "asc";
  } else {
    sortField.value = field;
    sortDirection.value = "asc";
  }
};

// 过滤和排序的设备列表
const filteredAndSortedDevices = computed(() => {
  let filtered = props.devices;

  // 搜索过滤
  if (props.searchQuery) {
    const query = props.searchQuery.toLowerCase();
    filtered = filtered.filter(
      device =>
        device.device_id?.toLowerCase().includes(query) ||
        device.brand?.toLowerCase().includes(query) ||
        device.model?.toLowerCase().includes(query)
    );
  }

  // 状态过滤
  if (props.statusFilter) {
    filtered = filtered.filter(device => device.status === props.statusFilter);
  }

  // 排序
  if (sortField.value) {
    filtered = [...filtered].sort((a, b) => {
      const aVal = a[sortField.value] || "";
      const bVal = b[sortField.value] || "";
      const result = aVal.toString().localeCompare(bVal.toString());
      return sortDirection.value === "asc" ? result : -result;
    });
  }

  return filtered;
});

// 查看日志
const handleViewLog = (device: DeviceItem) => {
  emit("view-log", device);
};

// 占用设备
const handleReserve = (device: DeviceItem) => {
  emit("reserve", device.id || device.device_id);
};

// 释放设备
const handleRelease = (device: DeviceItem) => {
  emit("release", device.id || device.device_id);
};

// 提醒占用者
const handleRemind = (device: DeviceItem) => {
  emit("remind", device);
};

// 连接设备
const handleConnect = (device: DeviceItem) => {
  emit("connect", device.id || device.device_id);
};

// 生成报告
const handleGenerateReport = (device: DeviceItem) => {
  reportDialogRef.value?.showDialog(device);
};

// 切换视图模式
const toggleViewMode = () => {
  const newMode = props.viewMode === "table" ? "card" : "table";
  emit("update:view-mode", newMode);
};

// 显示USB检查对话框
const showUsbCheck = () => {
  usbDialogRef.value?.showDialog();
};

// 开始编辑设备名称
const startEditName = (device: DeviceItem) => {
  editingDeviceId.value = device.id || device.device_id;
  editingName.value = device.name || device.device_id || "";
};

// 保存设备名称
const saveDeviceName = (device: DeviceItem) => {
  const newName = editingName.value.trim();
  if (newName) {
    emit("update-device-name", {
      id: device.id,
      name: newName,
      onsucceed: () => {
        device.name = newName;
      }
    });
  }
  cancelEditName();
};

// 取消编辑名称
const cancelEditName = () => {
  editingDeviceId.value = null;
  editingName.value = "";
};

// 判断是否正在编辑该设备的名称
const isEditingName = (device: DeviceItem) => {
  return editingDeviceId.value === (device.id || device.device_id);
};
</script>

<template>
  <div>
    <!-- 搜索和筛选工具栏 -->
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center space-x-4">
        <el-input
          :model-value="searchQuery"
          @update:model-value="emit('update:search-query', $event)"
          placeholder="搜索品牌、型号、设备ID..."
          :prefix-icon="Search"
          style="width: 300px"
          clearable
        />

        <el-button
          v-if="false"
          :icon="Connection"
          type="warning"
          @click="showUsbCheck"
        >
          USB检查
        </el-button>
      </div>

      <div class="flex items-center space-x-2">
        <el-button
          :icon="viewMode === 'table' ? Grid : List"
          @click="toggleViewMode"
        >
          {{ viewMode === "table" ? "卡片视图" : "表格视图" }}
        </el-button>

        <el-button
          v-if="false"
          :icon="Refresh"
          type="primary"
          @click="emit('refresh')"
        >
          刷新
        </el-button>
      </div>
    </div>

    <!-- 错误信息 -->
    <el-alert v-if="error" :title="error" type="error" class="mb-4" show-icon />

    <!-- 表格视图 -->
    <el-table
      v-if="viewMode === 'table'"
      :data="filteredAndSortedDevices"
      stripe
      style="width: 100%"
      empty-text="请连接设备后点击扫描按钮"
      class="devices-table"
      @row-dblclick="handleViewLog"
    >
      <el-table-column
        prop="status"
        label="状态"
        width="100"
        sortable
        @click="sortBy('status')"
      >
        <template #default="{ row }">
          <el-tag
            :type="getEnumEntry(deviceStatusEnum, row.status)?.type"
            effect="dark"
          >
            {{ getEnumEntry(deviceStatusEnum, row.status)?.label || "未知" }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column
        prop="device_id"
        label="设备ID"
        width="200"
        sortable
        @click="sortBy('device_id')"
      >
        <template #default="{ row }">
          <el-tag type="info" effect="plain">{{ row.device_id }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column
        prop="name"
        label="名称"
        width="240"
        sortable
        @click="sortBy('name')"
      >
        <template #default="{ row }">
          <div class="flex items-center">
            <!-- 编辑状态 -->
            <div
              v-if="isEditingName(row)"
              class="flex items-center space-x-1 w-full"
            >
              <el-input
                v-model="editingName"
                size="default"
                placeholder="请输入设备名称"
                @keyup.enter="saveDeviceName(row)"
                @keyup.esc="cancelEditName"
                style="flex: 1"
              />
              <el-button
                :icon="Check"
                type="success"
                size="small"
                circle
                @click="saveDeviceName(row)"
              />
              <el-button
                :icon="Close"
                type="danger"
                size="small"
                circle
                @click="cancelEditName"
              />
            </div>
            <!-- 显示状态 -->
            <div v-else class="flex items-center space-x-2 w-full">
              <span class="flex-1">{{
                row.name || row.device_id || "未命名"
              }}</span>
              <el-button
                :icon="Edit"
                type="primary"
                size="small"
                circle
                plain
                @click="startEditName(row)"
              />
            </div>
          </div>
        </template>
      </el-table-column>

      <el-table-column
        prop="model"
        label="型号"
        width="180"
        sortable
        @click="sortBy('model')"
      />

      <el-table-column
        prop="brand"
        label="品牌"
        sortable
        @click="sortBy('brand')"
      />

      <el-table-column
        prop="android_version"
        label="系统版本"
        width="180"
        sortable
        @click="sortBy('android_version')"
      >
        <template #default="{ row }">
          <el-tag v-if="row.android_version" type="info">
            {{ row.android_version }}
          </el-tag>
          <span v-else class="text-gray-400">-</span>
        </template>
      </el-table-column>

      <el-table-column
        prop="current_user_name"
        label="占用人员"
        sortable
        @click="sortBy('current_user_name')"
        width="140"
      >
        <template #default="{ row }">
          <el-tag v-if="row.current_user" type="warning" effect="dark">
            <span class="text-white">🔒{{ row.current_user_name }}</span>
          </el-tag>
          <span v-else class="text-gray-400">-</span>
        </template>
      </el-table-column>

      <el-table-column prop="resolution" label="分辨率">
        <template #default="{ row }">
          {{ row.resolution || "-" }}
        </template>
      </el-table-column>

      <el-table-column prop="ip_address" label="IP地址">
        <template #default="{ row }">
          {{ row.ip_address || "-" }}
        </template>
      </el-table-column>

      <el-table-column prop="last_online" label="最后在线">
        <template #default="{ row }">
          {{ TimeDefault(row.last_online) || "-" }}
        </template>
      </el-table-column>

      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <div class="flex space-x-1">
            <el-button
              v-if="
                !row.current_user &&
                row.status === deviceStatusEnum.ONLINE.value
              "
              :icon="Lock"
              type="warning"
              plain
              @click="handleReserve(row)"
            >
              占用
            </el-button>
            <el-button
              v-if="row.current_user_username === loginedUsername"
              :icon="Unlock"
              type="success"
              plain
              @click="handleRelease(row)"
            >
              释放
            </el-button>
            <el-button
              v-if="
                row.current_user &&
                row.current_user_username !== loginedUsername
              "
              :icon="Message"
              type="primary"
              plain
              @click.stop="handleRemind(row)"
            >
              提醒
            </el-button>

            <el-button
              v-if="false"
              type="success"
              :icon="Connection"
              :disabled="row.status === 'online' || row.status === 'device'"
              @click="handleConnect(row)"
            >
              连接
            </el-button>

            <el-button
              v-if="false"
              type="primary"
              :icon="Document"
              @click="handleGenerateReport(row)"
            >
              报告
            </el-button>

            <el-button v-if="false" type="info" :icon="Monitor" disabled>
              屏幕
            </el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <!-- 卡片视图 -->
    <div
      v-if="viewMode === 'card'"
      class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"
    >
      <el-card
        v-for="device in filteredAndSortedDevices"
        :key="device.id || device.device_id"
        shadow="hover"
        class="device-card"
      >
        <template #header>
          <div class="flex items-center justify-between">
            <div class="flex items-center">
              <div
                class="w-3 h-3 rounded-full mr-2"
                :class="{
                  'bg-green-500':
                    device.status === deviceStatusEnum.ONLINE.value,
                  'bg-red-500':
                    device.status === deviceStatusEnum.OFFLINE.value,
                  'bg-orange-500':
                    device.status === deviceStatusEnum.UNAUTHORIZED.value
                }"
              />
              <span class="font-medium">
                {{ device.brand }} {{ device.model }}
              </span>
            </div>
            <el-tag :type="getEnumEntry(deviceStatusEnum, device.status)?.type">
              {{
                getEnumEntry(deviceStatusEnum, device.status)?.label || "未知"
              }}
            </el-tag>
          </div>
        </template>
        <div class="mb-4">
          <div class="flex justify-between items-center mb-2">
            <span class="text-gray-500">名称:</span>
            <!-- 编辑状态 -->
            <div
              v-if="isEditingName(device)"
              class="flex items-center space-x-1"
            >
              <el-input
                v-model="editingName"
                size="small"
                placeholder="请输入设备名称"
                @keyup.enter="saveDeviceName(device)"
                @keyup.esc="cancelEditName"
                style="width: 120px"
              />
              <el-button
                :icon="Check"
                type="success"
                size="small"
                circle
                @click="saveDeviceName(device)"
              />
              <el-button
                :icon="Close"
                type="danger"
                size="small"
                circle
                @click="cancelEditName"
              />
            </div>
            <!-- 显示状态 -->
            <div v-else class="flex items-center space-x-1">
              <span>{{ device.name || device.device_id || "未命名" }}</span>
              <el-button
                :icon="Edit"
                type="primary"
                size="small"
                circle
                plain
                @click="startEditName(device)"
              />
            </div>
          </div>
          <div class="flex justify-between items-center">
            <span class="text-gray-500">占用人员:</span>
            <span>{{ device.current_user_name || "-" }}</span>
          </div>
          <div class="flex justify-between">
            <span class="text-gray-500">分辨率:</span>
            <span>{{ device.resolution || "-" }}</span>
          </div>
          <div class="flex justify-between">
            <span class="text-gray-500">IP地址:</span>
            <span>{{ device.ip_address || "-" }}</span>
          </div>
          <div class="flex justify-between">
            <span class="text-gray-500">最后在线:</span>
            <span>{{ TimeDefault(device.last_online) }}</span>
          </div>
        </div>

        <template #footer>
          <div class="flex justify-end space-x-2">
            <el-button
              v-if="
                !device.current_user &&
                device.status === deviceStatusEnum.ONLINE.value
              "
              :icon="Lock"
              type="warning"
              plain
              @click="handleReserve(device)"
            >
              占用
            </el-button>
            <el-button
              v-if="device.current_user_username === loginedUsername"
              :icon="Unlock"
              type="success"
              plain
              @click="handleRelease(device)"
            >
              释放
            </el-button>
            <el-button
              v-if="
                device.current_user &&
                device.current_user_username !== loginedUsername
              "
              :icon="Message"
              type="primary"
              plain
              @click.stop="handleRemind(device)"
            >
              提醒
            </el-button>
          </div>
        </template>
      </el-card>
    </div>

    <!-- 设备报告对话框 -->
    <!-- <DeviceReportDialog ref="reportDialogRef" /> -->

    <!-- USB检查对话框 -->
    <!-- <UsbCheckDialog ref="usbDialogRef" /> -->
  </div>
</template>

<style scoped>
.devices-table {
  border-radius: 8px;
  overflow: hidden;
}

.device-card {
  transition: all 0.3s ease;
}

.device-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 25px rgba(0, 0, 0, 0.1);
}
</style>
