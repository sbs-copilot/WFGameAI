import { ref, onMounted } from "vue";
import {
  actionTypeApi,
  actionParamApi,
  actionSort,
  type ActionTypeItem as ApiActionTypeItem,
  type ActionParamItem as ApiActionParamItem
} from "@/api/scripts";
import { ElMessage, ElMessageBox } from "element-plus";

// 2. 定义包含 UI 状态的扩展类型
export type ActionTypeItem = ApiActionTypeItem & { editing: boolean };
export type ActionParamItem = ApiActionParamItem & { editing: boolean };

export function useActionSettings() {
  const loading = ref(false);
  const actionTypes = ref<ActionTypeItem[]>([]);
  const activeCollapse = ref<string | string[]>([]);

  // 获取所有 Action Types
  const fetchActionTypes = async () => {
    loading.value = true;
    try {
      const res = await actionTypeApi.listWithParams();
      actionTypes.value = res.data.map(at => ({ ...at, editing: false }));
    } catch (error) {
      ElMessage.error("获取动作库列表失败");
      console.error(error);
    } finally {
      loading.value = false;
    }
  };

  onMounted(fetchActionTypes);

  // ================= ActionType 操作 =================

  // 添加新的 ActionType
  const handleAddActionType = () => {
    const newActionType: ActionTypeItem = {
      action_type: "new_action",
      name: "新动作",
      description: "",
      icon: "mdi:gesture-tap-button",
      is_enabled: true,
      params: [],
      editing: true // 标记为编辑状态
    };
    actionTypes.value.unshift(newActionType);
    activeCollapse.value = [newActionType.action_type]; // 自动展开
  };

  // 保存 ActionType (创建或更新)
  const handleSaveActionType = async (item: ActionTypeItem) => {
    try {
      if (item.id) {
        await actionTypeApi.update(item);
        ElMessage.success("更新成功");
      } else {
        const res = await actionTypeApi.create(item);
        item.id = res.data.id; // 更新ID
        ElMessage.success("创建成功");
      }
      item.editing = false;
    } catch (error) {
      ElMessage.error("保存失败");
      console.error(error);
    }
  };

  // 删除 ActionType
  const handleDeleteActionType = async (item: ActionTypeItem) => {
    await ElMessageBox.confirm(`确定删除动作 "${item.name}" 吗?`, "提示", {
      type: "warning"
    });
    try {
      if (item.id) {
        await actionTypeApi.delete(item.id);
      }
      const index = actionTypes.value.findIndex(at => at.id === item.id);
      if (index > -1) {
        actionTypes.value.splice(index, 1);
      }
      ElMessage.success("删除成功");
    } catch (error) {
      if (error !== "cancel") ElMessage.error("删除失败");
      console.error(error);
    }
  };

  // 取消编辑 ActionType
  const handleCancelEditActionType = (item: ActionTypeItem, index: number) => {
    if (!item.id) {
      actionTypes.value.splice(index, 1); // 如果是新建的，直接移除
    } else {
      item.editing = false; // 如果是已有的，恢复原状 (需要重新拉取或深拷贝)
      // 为了简单起见，我们这里只是取消编辑状态，更复杂的场景可能需要重置数据
    }
  };

  // ActionType 拖拽排序
  const onActionTypeSortEnd = async () => {
    const sorted_ids = actionTypes.value.map(at => at.id);
    try {
      await actionSort({ model: "action_type", sorted_ids });
      ElMessage.success("动作排序已更新");
    } catch (error) {
      ElMessage.error("动作排序更新失败");
      console.error(error);
      await fetchActionTypes(); // 失败时恢复顺序
    }
  };

  // ================= ActionParam 操作 =================

  // 添加新的 ActionParam
  const handleAddParam = (actionType: ActionTypeItem) => {
    if (!actionType.params) {
      actionType.params = [];
    }
    const newParam: ActionParamItem = {
      action_type: actionType.id,
      name: "新参数",
      type: "string",
      required: false,
      description: "",
      visible: true,
      editable: true,
      editing: true // 标记为编辑状态
    };
    // 插入到参数列表顶部
    actionType.params.unshift(newParam);
  };

  // 保存 ActionParam (创建或更新)
  const handleSaveParam = async (
    param: ActionParamItem,
    actionType: ActionTypeItem
  ) => {
    try {
      param.action_type = actionType.id; // 确保 action_type ID 正确

      // 构造提交数据，后端 ActionParamSerializer 需要 action_library 字段
      const submitData = { ...param, action_library: actionType.id };

      if (param.id) {
        await actionParamApi.update(submitData as any);
        ElMessage.success("参数更新成功");
      } else {
        const res = await actionParamApi.create(submitData as any);
        param.id = res.data.id; // 更新ID
        ElMessage.success("参数创建成功");
      }
      param.editing = false;
    } catch (error) {
      ElMessage.error("参数保存失败");
      console.error(error);
    }
  };

  // 删除 ActionParam
  const handleDeleteParam = async (
    param: ActionParamItem,
    actionType: ActionTypeItem
  ) => {
    await ElMessageBox.confirm(`确定删除参数 "${param.name}" 吗?`, "提示", {
      type: "warning"
    });
    try {
      if (param.id) {
        await actionParamApi.delete(param.id);
      }
      const index = actionType.params.findIndex(p => p.id === param.id);
      if (index > -1) {
        actionType.params.splice(index, 1);
      }
      ElMessage.success("参数删除成功");
    } catch (error) {
      if (error !== "cancel") ElMessage.error("参数删除失败");
      console.error(error);
    }
  };

  // 取消编辑 ActionParam
  const handleCancelEditParam = (
    param: ActionParamItem,
    actionType: ActionTypeItem,
    paramIndex: number
  ) => {
    if (!param.id) {
      actionType.params.splice(paramIndex, 1);
    } else {
      param.editing = false;
      // 可能需要恢复数据
    }
  };

  // ActionParam 拖拽排序
  const onParamSortEnd = async (actionType: ActionTypeItem) => {
    const sorted_ids = actionType.params.map(p => p.id);
    try {
      await actionSort({ model: "action_param", sorted_ids });
      ElMessage.success("参数排序已更新");
    } catch (error) {
      ElMessage.error("参数排序更新失败");
      console.error(error);
      // 失败时需要重新获取该 ActionType 的参数
    }
  };

  return {
    loading,
    actionTypes,
    activeCollapse,
    fetchActionTypes,
    handleAddActionType,
    handleSaveActionType,
    handleDeleteActionType,
    handleCancelEditActionType,
    onActionTypeSortEnd,
    handleAddParam,
    handleSaveParam,
    handleDeleteParam,
    handleCancelEditParam,
    onParamSortEnd
  };
}
