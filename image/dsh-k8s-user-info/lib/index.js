//#region lib/types/index.js
/**
 * 平台身份(k8s 网关注入)的 Host 半。
 *
 * dsh 的账号区硬编码为 DeepSeek 官方账号体系,平台侧(CAS)的用户身份
 * 无法进入其原生账号 UI。本插件把网关注入 Pod 环境的用户信息经
 * `webserver/index-inject` 事件序列化为启动全局变量,浏览器半
 * (lib/client.js)读取后渲染到侧栏底部。
 *
 * 配置由镜像内 patch.yml(dsh-entry 以 --patch 挂载)提供:
 *   userId:   !!js "process.env.DSH_K8S_USER_ID || ''"
 *   username: !!js "process.env.DSH_K8S_USERNAME || ''"
 * 环境缺失(非 k8s 网关部署)时两项为空,插件整体不生效。
 *
 * 注意不声明 Config:schema 校验需要 @deepseek-ai/schemastery,而插件被
 * 复制到 PVC 上的 DSH_HOME/node_modules(loader 的解析链),那里解析不到
 * dsh 安装树里的依赖;无 Config 时 Cordis 原样透传配置对象,
 * patch.yml 已保证值为字符串。
 */
//#endregion
//#region lib/index.js
/**
 * 把平台身份发布为启动全局变量(不含任何凭据)。
 * @param ctx - Host context collecting page initialization data.
 * @param config - deployment options: { userId, username }.
 */
function apply(ctx, config) {
	const userId = typeof config?.userId === "string" ? config.userId : "";
	const username = typeof config?.username === "string" ? config.username : "";
	if (!userId && !username) return;
	ctx.on("webserver/index-inject", (table) => {
		table.push({
			kind: "global",
			name: "__DSH_K8S_USER__",
			value: { userId, username }
		});
	});
}
//#endregion
export { apply };
