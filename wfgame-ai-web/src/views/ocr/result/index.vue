<template>
  <MainContent title="OCR 识别结果" :show-header="false">
    <template #header-extra>
      <div class="flex items-center gap-2">
        <el-divider direction="vertical" />

        <el-button
          v-if="false"
          class="ml-auto"
          type="primary"
          plain
          @click="startVerify"
        >
          审图模式
        </el-button>
        <!-- 返回按钮 -->
        <el-button class="ml-auto" type="text" @click="back()">
          返回
        </el-button>
      </div>
    </template>
    <div v-if="taskId" class="flex-full gap-4">
      <OcrTaskInfo
        class="flex-[1]"
        :task-id="taskId"
        v-model:show-translation="showTranslation"
        @refresh="handleRefresh"
      />
      <OcrResultList
        class="flex-[3]"
        v-if="taskId"
        :task-id="taskId"
        :key="refreshKey"
        :show-translation="showTranslation"
      />
    </div>
    <div v-else>
      <el-empty description="请传递有效的 Task Id" />
    </div>

    <OcrResultVerifyer
      v-if="taskId"
      v-model:visible="verifyVisible"
      :task-id="taskId"
      @refresh="handleRefresh"
    />
  </MainContent>
</template>
<script setup lang="ts">
import { ref, onMounted } from "vue";
import MainContent from "@/layout/components/mainContent/index.vue";
import OcrTaskInfo from "./components/OcrTaskInfo.vue";
import OcrResultList from "./components/OcrResultList.vue";
import OcrResultVerifyer from "./components/OcrResultVerifyer.vue";
import { useNavigate } from "@/views/common/utils/navHook";

defineOptions({
  name: "OcrResult"
});

const { getParameter, router } = useNavigate();
const taskId = ref<string | null>(null);
const verifyVisible = ref(false);
const refreshKey = ref(0);
const showTranslation = ref(true);

const startVerify = () => {
  verifyVisible.value = true;
};

const handleRefresh = () => {
  refreshKey.value++;
};

const back = () => {
  router.back();
};

onMounted(() => {
  const id = getParameter.id as string;
  taskId.value = id || "";
});
</script>

<style lang="scss" scoped></style>
