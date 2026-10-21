window.__ModuleLoader__.load({
	id: "dsh-k8s-user-info",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react_jsx_runtime = require("react/jsx-runtime");
		//#region lib/client/K8sUserChip.js
		/**
		* Render the platform identity chip in the sidebar footer.
		* Colors inherit from the surrounding theme; only layout is fixed.
		* @param props - Host-supplied wide flag (sidebar expanded vs icon rail).
		* @returns the user chip, or nothing when no identity was injected.
		*/
		function K8sUserChip({ wide }) {
			const info = globalThis["__DSH_K8S_USER__"];
			if (info == null || (!info.userId && !info.username)) return null;
			const label = info.username || info.userId;
			const initial = (label.trim()[0] || "?").toUpperCase();
			return (0, react_jsx_runtime.jsxs)("div", {
				title: info.userId || label,
				style: {
					display: "flex",
					alignItems: "center",
					gap: "8px",
					minWidth: 0,
					padding: "4px 8px",
					borderRadius: "8px",
					font: "12px/1.4 system-ui, -apple-system, sans-serif",
					opacity: 0.9,
					whiteSpace: "nowrap",
					overflow: "hidden",
					cursor: "default",
					userSelect: "none"
				},
				children: [
					(0, react_jsx_runtime.jsx)("span", {
						"aria-hidden": true,
						style: {
							flex: "0 0 auto",
							width: "20px",
							height: "20px",
							borderRadius: "50%",
							display: "inline-flex",
							alignItems: "center",
							justifyContent: "center",
							border: "1px solid currentColor",
							fontSize: "11px",
							fontWeight: 600,
							opacity: 0.8
						},
						children: initial
					}),
					wide
						? (0, react_jsx_runtime.jsx)("span", {
								style: { overflow: "hidden", textOverflow: "ellipsis" },
								children: label
							})
						: null
				]
			});
		}
		//#endregion
		//#region lib/client/index.js
		/** Required service: the UI slot registry. */
		const inject = ["slots"];
		/**
		* Fill one entry of the sidebar footer action list (kind: "list", so
		* multiple registrants coexist; ours is identified by its id and ordered
		* after any stock entries).
		* @param ctx - Client root context.
		*/
		function apply(ctx) {
			ctx.slots.inject("sidebar.footer.action", function* () {
				yield ctx.slots.register({
					name: "sidebar.footer.action",
					id: "dsh-k8s-user",
					order: 100
				}, K8sUserChip);
			});
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});
