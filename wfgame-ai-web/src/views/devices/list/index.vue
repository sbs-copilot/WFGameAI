<script setup lang="ts">
import { onMounted } from "vue";
import { Search } from "@element-plus/icons-vue";
import MainContent from "@/layout/components/mainContent/index.vue";
import DevicesStats from "./components/devicesStats.vue";
import DevicesTable from "./components/devicesTable.vue";
import DeviceLogDrawer from "./components/deviceLogDrawer.vue";
import { useDevicesManagement } from "./utils/hook";
import { useSSE, SSEEvent } from "@/layout/components/sseState/useSSE";
const { on } = useSSE();

defineOptions({
  name: "DevicesManagement"
});

const {
  devices,
  loading,
  error,
  stats,
  searchQuery,
  statusFilter,
  viewMode,
  filteredAndSortedDevices,
  fetchDevices,
  scanDevices,
  reserveDevice,
  releaseDevice,
  remindOccupant,
  updateDeviceName,
  logDrawerVisible,
  currentDeviceId,
  handleViewLog
} = useDevicesManagement();

onMounted(() => {
  fetchDevices();
  // 监听设备更新事件
  on(SSEEvent.DEVICE_UPDATE, () => {
    fetchDevices();
  });
});
</script>

<template>
  <MainContent title="设备管理" scroll-mode>
    <!-- 头部拓展功能 -->
    <template #header-extra>
      <div class="flex items-center justify-between">
        <div class="flex items-center space-x-2 ml-auto">
          <el-button
            :icon="Search"
            size="large"
            type="primary"
            plain
            @click="scanDevices"
          >
            扫描设备
          </el-button>
        </div>
      </div>
    </template>

    <!-- 页面内容 -->
    <div class="devices-content">
      <!-- 统计卡片 -->
      <DevicesStats
        :loading="false"
        :stats="stats"
        :status-filter="statusFilter"
        @update:status-filter="statusFilter = $event"
      />

      <!-- 设备表格/卡片视图 -->
      <DevicesTable
        :devices="devices"
        :loading="loading"
        :error="error"
        :stats="stats"
        :search-query="searchQuery"
        :status-filter="statusFilter"
        :view-mode="viewMode"
        :filtered-sorted-devices="filteredAndSortedDevices"
        @refresh="fetchDevices"
        @reserve="reserveDevice"
        @release="releaseDevice"
        @remind="remindOccupant"
        @update:search-query="searchQuery = $event"
        @update:view-mode="viewMode = $event"
        @update-device-name="updateDeviceName"
        @view-log="handleViewLog"
      />
    </div>

    <!-- 设备日志抽屉 -->
    <DeviceLogDrawer v-model="logDrawerVisible" :device-id="currentDeviceId" />
  </MainContent>
</template>

<style scoped lang="scss">
.devices-content {
  display: flex;
  flex-direction: column;
  gap: 20px;
  height: 100%;
}
</style>
