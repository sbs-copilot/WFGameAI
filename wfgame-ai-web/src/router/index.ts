import { getConfig } from "@/config";
import { useMultiTagsStoreHook } from "@/store/modules/multiTags";
import { usePermissionStoreHook } from "@/store/modules/permission";
import {
  type DataInfo,
  multipleTabsKey,
  removeToken,
  sessionKey
} from "@/utils/auth";
import NProgress from "@/utils/progress";
import "@/utils/sso";
import { buildHierarchyTree } from "@/utils/tree";
import { isAllEmpty, isUrl, openLink, storageLocal } from "@pureadmin/utils";
import Cookies from "js-cookie";
import {
  createRouter,
  RouteComponent,
  Router,
  RouteRecordRaw
} from "vue-router";
import remainingRouter from "./modules/remaining";
import {
  ascending,
  findRouteByPath,
  formatFlatteningRoutes,
  formatTwoStageRoutes,
  getHistoryMode,
  getTopMenu,
  handleAliveRoute,
  initRouter
} from "./utils";

/** 自动导入全部静态路由，无需再手动引入！匹配 src/router/modules 目录（任何嵌套级别）中具有 .ts 扩展名的所有文件，除了 remaining.ts 文件
 * 如何匹配所有文件请看：https://github.com/mrmlnc/fast-glob#basic-syntax
 * 如何排除文件请看：https://cn.vitejs.dev/guide/features.html#negative-patterns
 */
const modules: Record<string, any> = import.meta.glob(
  ["./modules/**/*.ts", "!./modules/**/remaining.ts"],
  {
    eager: true
  }
);

/** 原始静态路由（未做任何处理） */
const routes = [];

Object.keys(modules).forEach(key => {
  routes.push(modules[key].default);
});

/** 导出处理后的静态路由（三级及以上的路由全部拍成二级） */
export const constantRoutes: Array<RouteRecordRaw> = formatTwoStageRoutes(
  formatFlatteningRoutes(buildHierarchyTree(ascending(routes.flat(Infinity))))
);

/** 用于渲染菜单，保持原始层级 */
export const constantMenus: Array<RouteComponent> = ascending(
  routes.flat(Infinity)
).concat(...remainingRouter);

/** 不参与菜单的路由 */
export const remainingPaths = Object.keys(remainingRouter).map(v => {
  return remainingRouter[v].path;
});

/** 跳转到 SSO 登录 */
export const navigateToSSOLogin = () => {
  const { VITE_SSO_HOST } = import.meta.env;
  const currUrl = window.location.href;
  // 获取当前完整协议和域名（端口号）
  const url = new URL(currUrl);
  let nextPath = window.location.hash.replace(/^#/, ""); // 例如 /dashboard?foo=bar
  // 判断 nextPath 如果是 /login 或 /sso-login 则跳转到根目录 /
  if (nextPath.startsWith("/login") || nextPath.startsWith("/sso-login")) {
    nextPath = "/";
  }
  const redirect = encodeURIComponent(
    `${url.protocol}//${url.host}/#/login?next=${encodeURIComponent(nextPath)}`
  );
  window.location.href = `${VITE_SSO_HOST}/?redirect=${redirect}#/sso-login`;
};

/** 创建路由实例 */
export const router: Router = createRouter({
  history: getHistoryMode(import.meta.env.VITE_ROUTER_HISTORY),
  routes: constantRoutes.concat(...(remainingRouter as any)),
  strict: true,
  scrollBehavior(to, from, savedPosition) {
    return new Promise(resolve => {
      if (savedPosition) {
        return savedPosition;
      } else {
        if (from.meta.saveSrollTop) {
          const top: number =
            document.documentElement.scrollTop || document.body.scrollTop;
          resolve({ left: 0, top });
        }
      }
    });
  }
});

/** 重置路由 */
export function resetRouter() {
  router.getRoutes().forEach(route => {
    const { name, meta } = route;
    if (name && router.hasRoute(name) && meta?.backstage) {
      router.removeRoute(name);
      router.options.routes = formatTwoStageRoutes(
        formatFlatteningRoutes(
          buildHierarchyTree(ascending(routes.flat(Infinity)))
        )
      );
    }
  });
  usePermissionStoreHook().clearAllCachePage();
}

/** 路由白名单 */
const whiteList = ["/login"];
const errorPages = ["/error/403", "/error/404", "/error/500"];
const { VITE_HIDE_HOME } = import.meta.env;

router.beforeEach((to: toRouteType, _from, next) => {
  //   console.log("to", to);
  if (to.meta?.keepAlive) {
    handleAliveRoute(to, "add");
    // 页面整体刷新和点击标签页刷新
    if (_from.name === undefined || _from.name === "Redirect") {
      handleAliveRoute(to);
    }
  }
  const userInfo = storageLocal().getItem<DataInfo<number>>(sessionKey);
  function hasPerm() {
    // 免鉴权路由
    const noAuthRoutes = ["/welcome", "/replay"]; // 允许回放页无需鉴权
    if (noAuthRoutes.includes(to.fullPath)) {
      return true;
    }
    // 获取当前用户的 perms 权限
    const userPerms = userInfo?.perms || [];
    if (userPerms.length === 0) {
      return false;
    }
    // 超级权限
    if (userPerms.indexOf("*") > -1) {
      return true;
    }
    // 判断当前路由是否在用户权限列表中，不能用 fullPath，因为可能包含参数
    return !!userPerms.includes(to?.path as string);
  }
  // const userInfo = storageSession().getItem<DataInfo<number>>(sessionKey);
  NProgress.start();
  const externalLink = isUrl(to?.name as string);
  if (!externalLink) {
    to.matched.some(item => {
      if (!item.meta.title) return "";
      const Title = getConfig().Title;
      if (Title) document.title = `${item.meta.title} | ${Title}`;
      else document.title = item.meta.title as string;
    });
  }
  /** 如果已经登录并存在登录信息后不能跳转到路由白名单，而是继续保持在当前页面 */
  function toCorrectRoute() {
    whiteList.includes(to.fullPath) ? next(_from.fullPath) : next();
  }
  if (Cookies.get(multipleTabsKey) && userInfo) {
    // 无权限跳转403页面
    // if (to.meta?.roles && !isOneOfArray(to.meta?.roles, userInfo?.roles)) {
    //   next({ path: "/error/403" });
    // }

    if (!hasPerm() && !errorPages.includes(to.path)) {
      next({ path: "/error/403" });
    }
    // 开启隐藏首页后在浏览器地址栏手动输入首页welcome路由则跳转到404页面
    if (VITE_HIDE_HOME === "true" && to.fullPath === "/welcome") {
      next({ path: "/error/404" });
    }
    if (_from?.name) {
      // name为超链接
      if (externalLink) {
        openLink(to?.name as string);
        NProgress.done();
      } else {
        toCorrectRoute();
      }
    } else {
      // 刷新
      if (
        usePermissionStoreHook().wholeMenus.length === 0 &&
        to.path !== "/login"
      ) {
        initRouter().then((router: Router) => {
          if (!useMultiTagsStoreHook().getMultiTagsCache) {
            const { path } = to;
            const route = findRouteByPath(
              path,
              router.options.routes[0].children
            );
            getTopMenu(true);
            // query、params模式路由传参数的标签页不在此处处理
            if (route && route.meta?.title) {
              if (isAllEmpty(route.parentId) && route.meta?.backstage) {
                // 此处为动态顶级路由（目录）
                const { path, name, meta } = route.children[0];
                useMultiTagsStoreHook().handleTags("push", {
                  path,
                  name,
                  meta
                });
              } else {
                const { path, name, meta } = route;
                useMultiTagsStoreHook().handleTags("push", {
                  path,
                  name,
                  meta
                });
              }
            }
          }
          // 确保动态路由完全加入路由列表并且不影响静态路由（注意：动态路由刷新时router.beforeEach可能会触发两次，第一次触发动态路由还未完全添加，第二次动态路由才完全添加到路由列表，如果需要在router.beforeEach做一些判断可以在to.name存在的条件下去判断，这样就只会触发一次）
          if (isAllEmpty(to.name)) router.push(to.fullPath);
        });
      }
      toCorrectRoute();
    }
  } else {
    if (to.path !== "/login") {
      if (whiteList.indexOf(to.path) !== -1) {
        next();
      } else {
        removeToken();
        // next({ path: "/welcome" });
        navigateToSSOLogin();
        NProgress.done();
      }
    } else {
      next();
    }
  }
});

router.afterEach(() => {
  NProgress.done();
});

export default router;
