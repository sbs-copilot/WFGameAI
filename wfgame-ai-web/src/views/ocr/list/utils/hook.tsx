import { ref } from "vue";
import { ElMessageBox } from "element-plus";
import type {
  OcrRepository,
  OcrTask,
  OcrResult,
  OcrHistoryQuery
} from "@/api/ocr";
import { ocrRepositoryApi, ocrTaskApi } from "@/api/ocr";
import { superRequest } from "@/utils/request";
import { message } from "@/utils/message";

export const useOcr = () => {
  // 数据状态
  const repositories = ref<OcrRepository[]>([]);
  const tasks = ref<OcrTask[]>([]);
  const results = ref<OcrResult[]>([]);

  // ====================================================
  // [Git仓库] 相关方法
  const fetchRepositories = async () => {
    try {
      return await ocrRepositoryApi.list();
    } catch (error) {
      message("获取代码库列表失败", { type: "error" });
      console.error("fetchRepositories error:", error);
    }
  };

  const addRepository = async (
    projectId: string,
    repositoryData: { branch: string; url: string; token?: string }
  ) => {
    try {
      await ocrRepositoryApi.create({
        ...repositoryData
      });
      await fetchRepositories();
    } catch (error) {
      console.error("createRepository error:", error);
    }
  };

  const removeRepository = async (projectId: string, repositoryId: string) => {
    try {
      await ElMessageBox.confirm("确定要删除这个代码库吗？", "确认删除", {
        type: "warning"
      });
      await ocrRepositoryApi.delete(repositoryId);
      await fetchRepositories();
    } catch (error) {
      if (error !== "cancel") {
        console.error("deleteRepository error:", error);
      }
    }
  };

  // ====================================================

  // ====================================================
  // [Task] 相关方法
  const fetchTasks = async (params?: OcrHistoryQuery) => {
    try {
      const res = await superRequest({
        apiFunc: ocrTaskApi.history,
        apiParams: { ...params },
        enableSucceedMsg: false
      });
      return res;
    } catch (error) {
      message("获取任务列表失败", { type: "error" });
      console.error("fetchTasks error:", error);
    }
  };

  // [Task] 删除任务
  const deleteTask = async (taskId: string) => {
    try {
      await superRequest({
        apiFunc: ocrTaskApi.delete,
        apiParams: taskId,
        enableSucceedMsg: true
      });
    } catch (error) {
      message("删除任务失败", { type: "error" });
      console.error("deleteTask error:", error);
    }
  };

  // [downloadTaskByExportUrl] 通过任务的 export_url 字段下载任务结果
  const downloadTaskByExportUrl = (task: OcrTask) => {
    const url = task?.export_url || "";
    if (!url) {
      message(`当前任务未找到可下载的统计文件，请联系管理员！`, {
        type: "warning"
      });
      return;
    }
    window.open(url, "_blank");
  };

  // [Download] 下载任务结果
  const downloadTask = async (task: OcrTask) => {
    try {
      const taskId = task.id;
      const response: any = await ocrTaskApi.download(taskId, {
        timeout: 60000
      });
      // 创建一个下载链接
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement("a");

      // 从响应头获取文件名，或者使用默认文件名
      const contentDisposition = response.headers["content-disposition"];
      const fileNameMatch = contentDisposition?.match(/filename=(.+)/);
      const fileName = fileNameMatch ? fileNameMatch[1] : "export.xlsx";

      link.href = url;
      link.setAttribute("download", fileName);
      document.body.appendChild(link);
      link.click();

      // 清理
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } catch (error) {
      message("下载出错, 请在console中查看错误详情!", { type: "error" });
      console.error("downloadTask error:", error);
    }
  };

  return {
    // 状态
    repositories,
    tasks,
    results,

    // 代码库管理
    fetchRepositories,
    addRepository,
    removeRepository,

    // 任务管理
    fetchTasks,
    deleteTask,
    downloadTaskByExportUrl,
    downloadTask
  };
};
