<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { ElMessage } from "element-plus";
import { Right, Tools } from "@element-plus/icons-vue";
import RepoManager from "@/views/ocr/list/components/RepoManager.vue";
import {
  ocrTaskApi,
  ocrRepositoryApi,
  OcrRepository,
  type BindTransRepoParams
} from "@/api/ocr";
import { Delete } from "@element-plus/icons-vue";
import { superRequest } from "@/utils/request";

const props = defineProps<{
  modelValue: boolean;
  taskId: string;
  taskConfig: any;
}>();

const emit = defineEmits(["update:modelValue", "success"]);

const visible = computed({
  get: () => props.modelValue,
  set: val => emit("update:modelValue", val)
});

const loading = ref(false);
const repos = ref<OcrRepository[]>([]);
const branches = ref<string[]>([]);

const form = ref({
  repoId: "",
  branch: "",
  mappings: [{ source_subdir: "", trans_subdir: "" }]
});

const repoManagerVisible = ref(false);
const openRepoManager = () => {
  repoManagerVisible.value = true;
};

// Fetch available repositories
const fetchRepos = async () => {
  await superRequest({
    apiFunc: ocrRepositoryApi.list,
    onSucceed: data => {
      repos.value = data;
    }
  });
};

// Fetch branches when repo changes
const handleRepoChange = async () => {
  form.value.branch = "";
  branches.value = [];
  if (!form.value.repoId) return;

  await superRequest({
    apiFunc: ocrRepositoryApi.getBranches,
    apiParams: form.value.repoId,
    onSucceed: data => {
      branches.value = data?.branches || [];
      // Set default branch if available
      const repo = repos.value.find(r => r.id === form.value.repoId);
      if (repo && branches.value.includes(repo.branch)) {
        form.value.branch = repo.branch;
      } else if (branches.value.length > 0) {
        form.value.branch = branches.value[0];
      }
    }
  });
};

// Load source directories (lazy load)
const loadSourceDirs = async (node: any, resolve: any) => {
  const path = node.level === 0 ? "" : node.data.value;
  superRequest({
    apiFunc: ocrTaskApi.getSourceDirs,
    apiParams: { task_id: props.taskId, path },
    onSucceed: data => {
      const nodes = data.map((item: any) => ({
        ...item,
        name: item.label, // 保存短名用于树显示
        label: item.value // 将 label 设为完整路径用于输入框显示
      }));
      resolve(nodes);
    },
    onFailed: () => {
      resolve([]);
    }
  });
};

// Load translation repo directories (lazy load)
const loadTransDirs = async (node: any, resolve: any) => {
  if (!form.value.repoId || !form.value.branch) {
    return resolve([]);
  }

  const path = node.level === 0 ? "" : node.data.path;
  superRequest({
    apiFunc: ocrTaskApi.getTransRepoDirs,
    apiParams: {
      repo_id: form.value.repoId,
      branch: form.value.branch,
      path
    },
    onSucceed: data => {
      const nodes = data.map((item: any) => ({
        value: item.path,
        label: item.path, // 修改：label 使用完整路径
        name: item.name, // 新增：name 使用短名
        path: item.path,
        leaf: false // Assuming all returned items are directories and might have children
      }));
      resolve(nodes);
    },
    onFailed: () => {
      resolve([]);
    }
  });
};

const addMapping = () => {
  form.value.mappings.push({ source_subdir: "", trans_subdir: "" });
};

const removeMapping = (index: number) => {
  form.value.mappings.splice(index, 1);
};

const handleConfirm = async () => {
  if (!form.value.repoId || !form.value.branch) {
    ElMessage.warning("请选择翻译仓库和分支");
    return;
  }

  // Validate mappings
  for (const mapping of form.value.mappings) {
    if (!mapping.source_subdir || !mapping.trans_subdir) {
      ElMessage.warning("请完善目录映射配置");
      return;
    }
  }

  const repo = repos.value.find(r => r.id === form.value.repoId);
  if (!repo) return;

  loading.value = true;

  superRequest({
    apiFunc: ocrTaskApi.bindTransRepo,
    apiParams: {
      task_id: props.taskId,
      repo_id: form.value.repoId,
      branch: form.value.branch,
      mapping: form.value.mappings
    } as BindTransRepoParams,
    onSucceed: () => {
      visible.value = false;
      emit("success");
    },
    onCompleted: () => {
      loading.value = false;
    }
  });
};

watch(
  () => props.modelValue,
  val => {
    if (val) {
      fetchRepos();
    }
  }
);
</script>

<template>
  <el-dialog
    v-model="visible"
    title="🔗关联翻译资源"
    width="900px"
    :close-on-click-modal="false"
  >
    <el-form :model="form" label-width="100px">
      <el-row :gutter="0" class="split-layout">
        <!-- Left Panel: Source -->
        <el-col :span="12" class="panel-left">
          <div class="panel-header">本地 OCR 结果 (源)</div>
          <div class="panel-content">
            <el-form-item label="目标目录">
              <span class="info-text">{{ taskConfig.target_dir }}</span>
            </el-form-item>
            <el-form-item label="目标路径">
              <span class="info-text">{{ taskConfig.target_path }}</span>
            </el-form-item>
          </div>
        </el-col>

        <!-- Right Panel: Target -->
        <el-col :span="12" class="panel-right">
          <div class="panel-header">翻译仓库 (目标)</div>
          <div class="panel-content">
            <el-form-item label="翻译仓库">
              <div style="display: flex; width: 100%; gap: 8px">
                <el-select
                  v-model="form.repoId"
                  placeholder="选择仓库"
                  @change="handleRepoChange"
                  style="flex: 1"
                >
                  <el-option
                    v-for="item in repos"
                    :key="item.id"
                    :label="item.url"
                    :value="item.id"
                  />
                </el-select>
                <el-button :icon="Tools" @click="openRepoManager" />
              </div>
            </el-form-item>
            <el-form-item label="分支">
              <el-select
                v-model="form.branch"
                placeholder="选择分支"
                style="width: 100%"
              >
                <el-option
                  v-for="item in branches"
                  :key="item"
                  :label="item"
                  :value="item"
                />
              </el-select>
            </el-form-item>
          </div>
        </el-col>
      </el-row>

      <div class="mapping-container">
        <div class="mapping-header">
          <span>目录映射配置</span>
          <el-button type="primary" link @click="addMapping">
            + 添加映射
          </el-button>
        </div>

        <div
          v-for="(mapping, index) in form.mappings"
          :key="index"
          class="mapping-item"
        >
          <el-row :gutter="10" align="middle">
            <el-col :span="10">
              <el-tree-select
                v-model="mapping.source_subdir"
                :props="{
                  label: 'label',
                  children: 'children',
                  isLeaf: 'leaf'
                }"
                :load="loadSourceDirs"
                lazy
                placeholder="源目录 (OCR结果)"
                check-strictly
                :expand-on-click-node="false"
                style="width: 100%"
              >
                <template #default="{ data }">
                  <span>{{ data.name }}</span>
                </template>
              </el-tree-select>
            </el-col>
            <el-col :span="2" class="text-center">
              <el-icon><Right /></el-icon>
            </el-col>
            <el-col :span="10">
              <el-tree-select
                v-model="mapping.trans_subdir"
                :props="{
                  label: 'label',
                  children: 'children',
                  isLeaf: 'leaf'
                }"
                :load="loadTransDirs"
                lazy
                placeholder="翻译目录"
                check-strictly
                :expand-on-click-node="false"
                :key="form.repoId + form.branch"
                style="width: 100%"
              >
                <template #default="{ data }">
                  <span>{{ data.name }}</span>
                </template>
              </el-tree-select>
            </el-col>
            <el-col :span="2">
              <el-button
                type="danger"
                link
                :icon="Delete"
                @click="removeMapping(index)"
                v-if="form.mappings.length > 1"
              />
            </el-col>
          </el-row>
        </div>
      </div>
    </el-form>

    <template #footer>
      <span class="dialog-footer">
        <el-button @click="visible = false">取消</el-button>
        <el-button type="primary" @click="handleConfirm" :loading="loading">
          确认
        </el-button>
      </span>
    </template>

    <RepoManager
      v-model="repoManagerVisible"
      append-to-body
      @close="fetchRepos"
    />
  </el-dialog>
</template>

<style scoped>
.split-layout {
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: 20px;
}

.panel-left {
  background-color: #f5f7fa;
  border-right: 1px solid #dcdfe6;
}

.panel-right {
  background-color: #fff;
}

.panel-header {
  padding: 10px 15px;
  font-weight: bold;
  border-bottom: 1px solid #ebeef5;
  background-color: #fafafa;
}

.panel-content {
  padding: 15px;
}

.info-text {
  color: #606266;
  word-break: break-all;
}

.mapping-container {
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  padding: 16px;
  margin-top: 10px;
}
.mapping-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  font-weight: bold;
}
.mapping-item {
  margin-bottom: 10px;
}
.text-center {
  text-align: center;
}
</style>
