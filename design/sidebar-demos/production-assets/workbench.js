//#region \0rolldown/runtime.js
var e = Object.create, t = Object.defineProperty, n = Object.getOwnPropertyDescriptor, r = Object.getOwnPropertyNames, i = Object.getPrototypeOf, a = Object.prototype.hasOwnProperty, o = (e, t) => () => (t || (e((t = { exports: {} }).exports, t), e = null), t.exports), s = (e, n) => {
	let r = {};
	for (var i in e) t(r, i, {
		get: e[i],
		enumerable: !0
	});
	return n || t(r, Symbol.toStringTag, { value: "Module" }), r;
}, c = (e, i, o, s) => {
	if (i && typeof i == "object" || typeof i == "function") for (var c = r(i), l = 0, u = c.length, d; l < u; l++) d = c[l], !a.call(e, d) && d !== o && t(e, d, {
		get: ((e) => i[e]).bind(null, d),
		enumerable: !(s = n(i, d)) || s.enumerable
	});
	return e;
}, l = (n, r, a) => (a = n == null ? {} : e(i(n)), c(r || !n || !n.__esModule ? t(a, "default", {
	value: n,
	enumerable: !0
}) : a, n)), u = /* @__PURE__ */ o(((e) => {
	function t(e, t) {
		var n = e.length;
		e.push(t);
		a: for (; 0 < n;) {
			var r = n - 1 >>> 1, a = e[r];
			if (0 < i(a, t)) e[r] = t, e[n] = a, n = r;
			else break a;
		}
	}
	function n(e) {
		return e.length === 0 ? null : e[0];
	}
	function r(e) {
		if (e.length === 0) return null;
		var t = e[0], n = e.pop();
		if (n !== t) {
			e[0] = n;
			a: for (var r = 0, a = e.length, o = a >>> 1; r < o;) {
				var s = 2 * (r + 1) - 1, c = e[s], l = s + 1, u = e[l];
				if (0 > i(c, n)) l < a && 0 > i(u, c) ? (e[r] = u, e[l] = n, r = l) : (e[r] = c, e[s] = n, r = s);
				else if (l < a && 0 > i(u, n)) e[r] = u, e[l] = n, r = l;
				else break a;
			}
		}
		return t;
	}
	function i(e, t) {
		var n = e.sortIndex - t.sortIndex;
		return n === 0 ? e.id - t.id : n;
	}
	if (e.unstable_now = void 0, typeof performance == "object" && typeof performance.now == "function") {
		var a = performance;
		e.unstable_now = function() {
			return a.now();
		};
	} else {
		var o = Date, s = o.now();
		e.unstable_now = function() {
			return o.now() - s;
		};
	}
	var c = [], l = [], u = 1, d = null, f = 3, p = !1, m = !1, h = !1, g = !1, _ = typeof setTimeout == "function" ? setTimeout : null, v = typeof clearTimeout == "function" ? clearTimeout : null, y = typeof setImmediate < "u" ? setImmediate : null;
	function b(e) {
		for (var i = n(l); i !== null;) {
			if (i.callback === null) r(l);
			else if (i.startTime <= e) r(l), i.sortIndex = i.expirationTime, t(c, i);
			else break;
			i = n(l);
		}
	}
	function x(e) {
		if (h = !1, b(e), !m) if (n(c) !== null) m = !0, S || (S = !0, D());
		else {
			var t = n(l);
			t !== null && re(x, t.startTime - e);
		}
	}
	var S = !1, C = -1, w = 5, T = -1;
	function E() {
		return g ? !0 : !(e.unstable_now() - T < w);
	}
	function ee() {
		if (g = !1, S) {
			var t = e.unstable_now();
			T = t;
			var i = !0;
			try {
				a: {
					m = !1, h && (h = !1, v(C), C = -1), p = !0;
					var a = f;
					try {
						b: {
							for (b(t), d = n(c); d !== null && !(d.expirationTime > t && E());) {
								var o = d.callback;
								if (typeof o == "function") {
									d.callback = null, f = d.priorityLevel;
									var s = o(d.expirationTime <= t);
									if (t = e.unstable_now(), typeof s == "function") {
										d.callback = s, b(t), i = !0;
										break b;
									}
									d === n(c) && r(c), b(t);
								} else r(c);
								d = n(c);
							}
							if (d !== null) i = !0;
							else {
								var u = n(l);
								u !== null && re(x, u.startTime - t), i = !1;
							}
						}
						break a;
					} finally {
						d = null, f = a, p = !1;
					}
					i = void 0;
				}
			} finally {
				i ? D() : S = !1;
			}
		}
	}
	var D;
	if (typeof y == "function") D = function() {
		y(ee);
	};
	else if (typeof MessageChannel < "u") {
		var te = new MessageChannel(), ne = te.port2;
		te.port1.onmessage = ee, D = function() {
			ne.postMessage(null);
		};
	} else D = function() {
		_(ee, 0);
	};
	function re(t, n) {
		C = _(function() {
			t(e.unstable_now());
		}, n);
	}
	e.unstable_IdlePriority = 5, e.unstable_ImmediatePriority = 1, e.unstable_LowPriority = 4, e.unstable_NormalPriority = 3, e.unstable_Profiling = null, e.unstable_UserBlockingPriority = 2, e.unstable_cancelCallback = function(e) {
		e.callback = null;
	}, e.unstable_forceFrameRate = function(e) {
		0 > e || 125 < e ? console.error("forceFrameRate takes a positive int between 0 and 125, forcing frame rates higher than 125 fps is not supported") : w = 0 < e ? Math.floor(1e3 / e) : 5;
	}, e.unstable_getCurrentPriorityLevel = function() {
		return f;
	}, e.unstable_next = function(e) {
		switch (f) {
			case 1:
			case 2:
			case 3:
				var t = 3;
				break;
			default: t = f;
		}
		var n = f;
		f = t;
		try {
			return e();
		} finally {
			f = n;
		}
	}, e.unstable_requestPaint = function() {
		g = !0;
	}, e.unstable_runWithPriority = function(e, t) {
		switch (e) {
			case 1:
			case 2:
			case 3:
			case 4:
			case 5: break;
			default: e = 3;
		}
		var n = f;
		f = e;
		try {
			return t();
		} finally {
			f = n;
		}
	}, e.unstable_scheduleCallback = function(r, i, a) {
		var o = e.unstable_now();
		switch (typeof a == "object" && a ? (a = a.delay, a = typeof a == "number" && 0 < a ? o + a : o) : a = o, r) {
			case 1:
				var s = -1;
				break;
			case 2:
				s = 250;
				break;
			case 5:
				s = 1073741823;
				break;
			case 4:
				s = 1e4;
				break;
			default: s = 5e3;
		}
		return s = a + s, r = {
			id: u++,
			callback: i,
			priorityLevel: r,
			startTime: a,
			expirationTime: s,
			sortIndex: -1
		}, a > o ? (r.sortIndex = a, t(l, r), n(c) === null && r === n(l) && (h ? (v(C), C = -1) : h = !0, re(x, a - o))) : (r.sortIndex = s, t(c, r), m || p || (m = !0, S || (S = !0, D()))), r;
	}, e.unstable_shouldYield = E, e.unstable_wrapCallback = function(e) {
		var t = f;
		return function() {
			var n = f;
			f = t;
			try {
				return e.apply(this, arguments);
			} finally {
				f = n;
			}
		};
	};
})), d = /* @__PURE__ */ o(((e, t) => {
	t.exports = u();
})), f = /* @__PURE__ */ o(((e) => {
	var t = Symbol.for("react.transitional.element"), n = Symbol.for("react.portal"), r = Symbol.for("react.fragment"), i = Symbol.for("react.strict_mode"), a = Symbol.for("react.profiler"), o = Symbol.for("react.consumer"), s = Symbol.for("react.context"), c = Symbol.for("react.forward_ref"), l = Symbol.for("react.suspense"), u = Symbol.for("react.memo"), d = Symbol.for("react.lazy"), f = Symbol.for("react.activity"), p = Symbol.iterator;
	function m(e) {
		return typeof e != "object" || !e ? null : (e = p && e[p] || e["@@iterator"], typeof e == "function" ? e : null);
	}
	var h = {
		isMounted: function() {
			return !1;
		},
		enqueueForceUpdate: function() {},
		enqueueReplaceState: function() {},
		enqueueSetState: function() {}
	}, g = Object.assign, _ = {};
	function v(e, t, n) {
		this.props = e, this.context = t, this.refs = _, this.updater = n || h;
	}
	v.prototype.isReactComponent = {}, v.prototype.setState = function(e, t) {
		if (typeof e != "object" && typeof e != "function" && e != null) throw Error("takes an object of state variables to update or a function which returns an object of state variables.");
		this.updater.enqueueSetState(this, e, t, "setState");
	}, v.prototype.forceUpdate = function(e) {
		this.updater.enqueueForceUpdate(this, e, "forceUpdate");
	};
	function y() {}
	y.prototype = v.prototype;
	function b(e, t, n) {
		this.props = e, this.context = t, this.refs = _, this.updater = n || h;
	}
	var x = b.prototype = new y();
	x.constructor = b, g(x, v.prototype), x.isPureReactComponent = !0;
	var S = Array.isArray;
	function C() {}
	var w = {
		H: null,
		A: null,
		T: null,
		S: null
	}, T = Object.prototype.hasOwnProperty;
	function E(e, n, r) {
		var i = r.ref;
		return {
			$$typeof: t,
			type: e,
			key: n,
			ref: i === void 0 ? null : i,
			props: r
		};
	}
	function ee(e, t) {
		return E(e.type, t, e.props);
	}
	function D(e) {
		return typeof e == "object" && !!e && e.$$typeof === t;
	}
	function te(e) {
		var t = {
			"=": "=0",
			":": "=2"
		};
		return "$" + e.replace(/[=:]/g, function(e) {
			return t[e];
		});
	}
	var ne = /\/+/g;
	function re(e, t) {
		return typeof e == "object" && e && e.key != null ? te("" + e.key) : t.toString(36);
	}
	function ie(e) {
		switch (e.status) {
			case "fulfilled": return e.value;
			case "rejected": throw e.reason;
			default: switch (typeof e.status == "string" ? e.then(C, C) : (e.status = "pending", e.then(function(t) {
				e.status === "pending" && (e.status = "fulfilled", e.value = t);
			}, function(t) {
				e.status === "pending" && (e.status = "rejected", e.reason = t);
			})), e.status) {
				case "fulfilled": return e.value;
				case "rejected": throw e.reason;
			}
		}
		throw e;
	}
	function ae(e, r, i, a, o) {
		var s = typeof e;
		(s === "undefined" || s === "boolean") && (e = null);
		var c = !1;
		if (e === null) c = !0;
		else switch (s) {
			case "bigint":
			case "string":
			case "number":
				c = !0;
				break;
			case "object": switch (e.$$typeof) {
				case t:
				case n:
					c = !0;
					break;
				case d: return c = e._init, ae(c(e._payload), r, i, a, o);
			}
		}
		if (c) return o = o(e), c = a === "" ? "." + re(e, 0) : a, S(o) ? (i = "", c != null && (i = c.replace(ne, "$&/") + "/"), ae(o, r, i, "", function(e) {
			return e;
		})) : o != null && (D(o) && (o = ee(o, i + (o.key == null || e && e.key === o.key ? "" : ("" + o.key).replace(ne, "$&/") + "/") + c)), r.push(o)), 1;
		c = 0;
		var l = a === "" ? "." : a + ":";
		if (S(e)) for (var u = 0; u < e.length; u++) a = e[u], s = l + re(a, u), c += ae(a, r, i, s, o);
		else if (u = m(e), typeof u == "function") for (e = u.call(e), u = 0; !(a = e.next()).done;) a = a.value, s = l + re(a, u++), c += ae(a, r, i, s, o);
		else if (s === "object") {
			if (typeof e.then == "function") return ae(ie(e), r, i, a, o);
			throw r = String(e), Error("Objects are not valid as a React child (found: " + (r === "[object Object]" ? "object with keys {" + Object.keys(e).join(", ") + "}" : r) + "). If you meant to render a collection of children, use an array instead.");
		}
		return c;
	}
	function oe(e, t, n) {
		if (e == null) return e;
		var r = [], i = 0;
		return ae(e, r, "", "", function(e) {
			return t.call(n, e, i++);
		}), r;
	}
	function se(e) {
		if (e._status === -1) {
			var t = e._result;
			t = t(), t.then(function(t) {
				(e._status === 0 || e._status === -1) && (e._status = 1, e._result = t);
			}, function(t) {
				(e._status === 0 || e._status === -1) && (e._status = 2, e._result = t);
			}), e._status === -1 && (e._status = 0, e._result = t);
		}
		if (e._status === 1) return e._result.default;
		throw e._result;
	}
	var O = typeof reportError == "function" ? reportError : function(e) {
		if (typeof window == "object" && typeof window.ErrorEvent == "function") {
			var t = new window.ErrorEvent("error", {
				bubbles: !0,
				cancelable: !0,
				message: typeof e == "object" && e && typeof e.message == "string" ? String(e.message) : String(e),
				error: e
			});
			if (!window.dispatchEvent(t)) return;
		} else if (typeof process == "object" && typeof process.emit == "function") {
			process.emit("uncaughtException", e);
			return;
		}
		console.error(e);
	}, k = {
		map: oe,
		forEach: function(e, t, n) {
			oe(e, function() {
				t.apply(this, arguments);
			}, n);
		},
		count: function(e) {
			var t = 0;
			return oe(e, function() {
				t++;
			}), t;
		},
		toArray: function(e) {
			return oe(e, function(e) {
				return e;
			}) || [];
		},
		only: function(e) {
			if (!D(e)) throw Error("React.Children.only expected to receive a single React element child.");
			return e;
		}
	};
	e.Activity = f, e.Children = k, e.Component = v, e.Fragment = r, e.Profiler = a, e.PureComponent = b, e.StrictMode = i, e.Suspense = l, e.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE = w, e.__COMPILER_RUNTIME = {
		__proto__: null,
		c: function(e) {
			return w.H.useMemoCache(e);
		}
	}, e.cache = function(e) {
		return function() {
			return e.apply(null, arguments);
		};
	}, e.cacheSignal = function() {
		return null;
	}, e.cloneElement = function(e, t, n) {
		if (e == null) throw Error("The argument must be a React element, but you passed " + e + ".");
		var r = g({}, e.props), i = e.key;
		if (t != null) for (a in t.key !== void 0 && (i = "" + t.key), t) !T.call(t, a) || a === "key" || a === "__self" || a === "__source" || a === "ref" && t.ref === void 0 || (r[a] = t[a]);
		var a = arguments.length - 2;
		if (a === 1) r.children = n;
		else if (1 < a) {
			for (var o = Array(a), s = 0; s < a; s++) o[s] = arguments[s + 2];
			r.children = o;
		}
		return E(e.type, i, r);
	}, e.createContext = function(e) {
		return e = {
			$$typeof: s,
			_currentValue: e,
			_currentValue2: e,
			_threadCount: 0,
			Provider: null,
			Consumer: null
		}, e.Provider = e, e.Consumer = {
			$$typeof: o,
			_context: e
		}, e;
	}, e.createElement = function(e, t, n) {
		var r, i = {}, a = null;
		if (t != null) for (r in t.key !== void 0 && (a = "" + t.key), t) T.call(t, r) && r !== "key" && r !== "__self" && r !== "__source" && (i[r] = t[r]);
		var o = arguments.length - 2;
		if (o === 1) i.children = n;
		else if (1 < o) {
			for (var s = Array(o), c = 0; c < o; c++) s[c] = arguments[c + 2];
			i.children = s;
		}
		if (e && e.defaultProps) for (r in o = e.defaultProps, o) i[r] === void 0 && (i[r] = o[r]);
		return E(e, a, i);
	}, e.createRef = function() {
		return { current: null };
	}, e.forwardRef = function(e) {
		return {
			$$typeof: c,
			render: e
		};
	}, e.isValidElement = D, e.lazy = function(e) {
		return {
			$$typeof: d,
			_payload: {
				_status: -1,
				_result: e
			},
			_init: se
		};
	}, e.memo = function(e, t) {
		return {
			$$typeof: u,
			type: e,
			compare: t === void 0 ? null : t
		};
	}, e.startTransition = function(e) {
		var t = w.T, n = {};
		w.T = n;
		try {
			var r = e(), i = w.S;
			i !== null && i(n, r), typeof r == "object" && r && typeof r.then == "function" && r.then(C, O);
		} catch (e) {
			O(e);
		} finally {
			t !== null && n.types !== null && (t.types = n.types), w.T = t;
		}
	}, e.unstable_useCacheRefresh = function() {
		return w.H.useCacheRefresh();
	}, e.use = function(e) {
		return w.H.use(e);
	}, e.useActionState = function(e, t, n) {
		return w.H.useActionState(e, t, n);
	}, e.useCallback = function(e, t) {
		return w.H.useCallback(e, t);
	}, e.useContext = function(e) {
		return w.H.useContext(e);
	}, e.useDebugValue = function() {}, e.useDeferredValue = function(e, t) {
		return w.H.useDeferredValue(e, t);
	}, e.useEffect = function(e, t) {
		return w.H.useEffect(e, t);
	}, e.useEffectEvent = function(e) {
		return w.H.useEffectEvent(e);
	}, e.useId = function() {
		return w.H.useId();
	}, e.useImperativeHandle = function(e, t, n) {
		return w.H.useImperativeHandle(e, t, n);
	}, e.useInsertionEffect = function(e, t) {
		return w.H.useInsertionEffect(e, t);
	}, e.useLayoutEffect = function(e, t) {
		return w.H.useLayoutEffect(e, t);
	}, e.useMemo = function(e, t) {
		return w.H.useMemo(e, t);
	}, e.useOptimistic = function(e, t) {
		return w.H.useOptimistic(e, t);
	}, e.useReducer = function(e, t, n) {
		return w.H.useReducer(e, t, n);
	}, e.useRef = function(e) {
		return w.H.useRef(e);
	}, e.useState = function(e) {
		return w.H.useState(e);
	}, e.useSyncExternalStore = function(e, t, n) {
		return w.H.useSyncExternalStore(e, t, n);
	}, e.useTransition = function() {
		return w.H.useTransition();
	}, e.version = "19.2.6";
})), p = /* @__PURE__ */ o(((e, t) => {
	t.exports = f();
})), m = /* @__PURE__ */ o(((e) => {
	var t = p();
	function n(e) {
		var t = "https://react.dev/errors/" + e;
		if (1 < arguments.length) {
			t += "?args[]=" + encodeURIComponent(arguments[1]);
			for (var n = 2; n < arguments.length; n++) t += "&args[]=" + encodeURIComponent(arguments[n]);
		}
		return "Minified React error #" + e + "; visit " + t + " for the full message or use the non-minified dev environment for full errors and additional helpful warnings.";
	}
	function r() {}
	var i = {
		d: {
			f: r,
			r: function() {
				throw Error(n(522));
			},
			D: r,
			C: r,
			L: r,
			m: r,
			X: r,
			S: r,
			M: r
		},
		p: 0,
		findDOMNode: null
	}, a = Symbol.for("react.portal");
	function o(e, t, n) {
		var r = 3 < arguments.length && arguments[3] !== void 0 ? arguments[3] : null;
		return {
			$$typeof: a,
			key: r == null ? null : "" + r,
			children: e,
			containerInfo: t,
			implementation: n
		};
	}
	var s = t.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
	function c(e, t) {
		if (e === "font") return "";
		if (typeof t == "string") return t === "use-credentials" ? t : "";
	}
	e.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE = i, e.createPortal = function(e, t) {
		var r = 2 < arguments.length && arguments[2] !== void 0 ? arguments[2] : null;
		if (!t || t.nodeType !== 1 && t.nodeType !== 9 && t.nodeType !== 11) throw Error(n(299));
		return o(e, t, null, r);
	}, e.flushSync = function(e) {
		var t = s.T, n = i.p;
		try {
			if (s.T = null, i.p = 2, e) return e();
		} finally {
			s.T = t, i.p = n, i.d.f();
		}
	}, e.preconnect = function(e, t) {
		typeof e == "string" && (t ? (t = t.crossOrigin, t = typeof t == "string" ? t === "use-credentials" ? t : "" : void 0) : t = null, i.d.C(e, t));
	}, e.prefetchDNS = function(e) {
		typeof e == "string" && i.d.D(e);
	}, e.preinit = function(e, t) {
		if (typeof e == "string" && t && typeof t.as == "string") {
			var n = t.as, r = c(n, t.crossOrigin), a = typeof t.integrity == "string" ? t.integrity : void 0, o = typeof t.fetchPriority == "string" ? t.fetchPriority : void 0;
			n === "style" ? i.d.S(e, typeof t.precedence == "string" ? t.precedence : void 0, {
				crossOrigin: r,
				integrity: a,
				fetchPriority: o
			}) : n === "script" && i.d.X(e, {
				crossOrigin: r,
				integrity: a,
				fetchPriority: o,
				nonce: typeof t.nonce == "string" ? t.nonce : void 0
			});
		}
	}, e.preinitModule = function(e, t) {
		if (typeof e == "string") if (typeof t == "object" && t) {
			if (t.as == null || t.as === "script") {
				var n = c(t.as, t.crossOrigin);
				i.d.M(e, {
					crossOrigin: n,
					integrity: typeof t.integrity == "string" ? t.integrity : void 0,
					nonce: typeof t.nonce == "string" ? t.nonce : void 0
				});
			}
		} else t ?? i.d.M(e);
	}, e.preload = function(e, t) {
		if (typeof e == "string" && typeof t == "object" && t && typeof t.as == "string") {
			var n = t.as, r = c(n, t.crossOrigin);
			i.d.L(e, n, {
				crossOrigin: r,
				integrity: typeof t.integrity == "string" ? t.integrity : void 0,
				nonce: typeof t.nonce == "string" ? t.nonce : void 0,
				type: typeof t.type == "string" ? t.type : void 0,
				fetchPriority: typeof t.fetchPriority == "string" ? t.fetchPriority : void 0,
				referrerPolicy: typeof t.referrerPolicy == "string" ? t.referrerPolicy : void 0,
				imageSrcSet: typeof t.imageSrcSet == "string" ? t.imageSrcSet : void 0,
				imageSizes: typeof t.imageSizes == "string" ? t.imageSizes : void 0,
				media: typeof t.media == "string" ? t.media : void 0
			});
		}
	}, e.preloadModule = function(e, t) {
		if (typeof e == "string") if (t) {
			var n = c(t.as, t.crossOrigin);
			i.d.m(e, {
				as: typeof t.as == "string" && t.as !== "script" ? t.as : void 0,
				crossOrigin: n,
				integrity: typeof t.integrity == "string" ? t.integrity : void 0
			});
		} else i.d.m(e);
	}, e.requestFormReset = function(e) {
		i.d.r(e);
	}, e.unstable_batchedUpdates = function(e, t) {
		return e(t);
	}, e.useFormState = function(e, t, n) {
		return s.H.useFormState(e, t, n);
	}, e.useFormStatus = function() {
		return s.H.useHostTransitionStatus();
	}, e.version = "19.2.6";
})), h = /* @__PURE__ */ o(((e, t) => {
	function n() {
		if (!(typeof __REACT_DEVTOOLS_GLOBAL_HOOK__ > "u" || typeof __REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE != "function")) try {
			__REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE(n);
		} catch (e) {
			console.error(e);
		}
	}
	n(), t.exports = m();
})), g = /* @__PURE__ */ o(((e) => {
	var t = d(), n = p(), r = h();
	function i(e) {
		var t = "https://react.dev/errors/" + e;
		if (1 < arguments.length) {
			t += "?args[]=" + encodeURIComponent(arguments[1]);
			for (var n = 2; n < arguments.length; n++) t += "&args[]=" + encodeURIComponent(arguments[n]);
		}
		return "Minified React error #" + e + "; visit " + t + " for the full message or use the non-minified dev environment for full errors and additional helpful warnings.";
	}
	function a(e) {
		return !(!e || e.nodeType !== 1 && e.nodeType !== 9 && e.nodeType !== 11);
	}
	function o(e) {
		var t = e, n = e;
		if (e.alternate) for (; t.return;) t = t.return;
		else {
			e = t;
			do
				t = e, t.flags & 4098 && (n = t.return), e = t.return;
			while (e);
		}
		return t.tag === 3 ? n : null;
	}
	function s(e) {
		if (e.tag === 13) {
			var t = e.memoizedState;
			if (t === null && (e = e.alternate, e !== null && (t = e.memoizedState)), t !== null) return t.dehydrated;
		}
		return null;
	}
	function c(e) {
		if (e.tag === 31) {
			var t = e.memoizedState;
			if (t === null && (e = e.alternate, e !== null && (t = e.memoizedState)), t !== null) return t.dehydrated;
		}
		return null;
	}
	function l(e) {
		if (o(e) !== e) throw Error(i(188));
	}
	function u(e) {
		var t = e.alternate;
		if (!t) {
			if (t = o(e), t === null) throw Error(i(188));
			return t === e ? e : null;
		}
		for (var n = e, r = t;;) {
			var a = n.return;
			if (a === null) break;
			var s = a.alternate;
			if (s === null) {
				if (r = a.return, r !== null) {
					n = r;
					continue;
				}
				break;
			}
			if (a.child === s.child) {
				for (s = a.child; s;) {
					if (s === n) return l(a), e;
					if (s === r) return l(a), t;
					s = s.sibling;
				}
				throw Error(i(188));
			}
			if (n.return !== r.return) n = a, r = s;
			else {
				for (var c = !1, u = a.child; u;) {
					if (u === n) {
						c = !0, n = a, r = s;
						break;
					}
					if (u === r) {
						c = !0, r = a, n = s;
						break;
					}
					u = u.sibling;
				}
				if (!c) {
					for (u = s.child; u;) {
						if (u === n) {
							c = !0, n = s, r = a;
							break;
						}
						if (u === r) {
							c = !0, r = s, n = a;
							break;
						}
						u = u.sibling;
					}
					if (!c) throw Error(i(189));
				}
			}
			if (n.alternate !== r) throw Error(i(190));
		}
		if (n.tag !== 3) throw Error(i(188));
		return n.stateNode.current === n ? e : t;
	}
	function f(e) {
		var t = e.tag;
		if (t === 5 || t === 26 || t === 27 || t === 6) return e;
		for (e = e.child; e !== null;) {
			if (t = f(e), t !== null) return t;
			e = e.sibling;
		}
		return null;
	}
	var m = Object.assign, g = Symbol.for("react.element"), _ = Symbol.for("react.transitional.element"), v = Symbol.for("react.portal"), y = Symbol.for("react.fragment"), b = Symbol.for("react.strict_mode"), x = Symbol.for("react.profiler"), S = Symbol.for("react.consumer"), C = Symbol.for("react.context"), w = Symbol.for("react.forward_ref"), T = Symbol.for("react.suspense"), E = Symbol.for("react.suspense_list"), ee = Symbol.for("react.memo"), D = Symbol.for("react.lazy"), te = Symbol.for("react.activity"), ne = Symbol.for("react.memo_cache_sentinel"), re = Symbol.iterator;
	function ie(e) {
		return typeof e != "object" || !e ? null : (e = re && e[re] || e["@@iterator"], typeof e == "function" ? e : null);
	}
	var ae = Symbol.for("react.client.reference");
	function oe(e) {
		if (e == null) return null;
		if (typeof e == "function") return e.$$typeof === ae ? null : e.displayName || e.name || null;
		if (typeof e == "string") return e;
		switch (e) {
			case y: return "Fragment";
			case x: return "Profiler";
			case b: return "StrictMode";
			case T: return "Suspense";
			case E: return "SuspenseList";
			case te: return "Activity";
		}
		if (typeof e == "object") switch (e.$$typeof) {
			case v: return "Portal";
			case C: return e.displayName || "Context";
			case S: return (e._context.displayName || "Context") + ".Consumer";
			case w:
				var t = e.render;
				return e = e.displayName, e ||= (e = t.displayName || t.name || "", e === "" ? "ForwardRef" : "ForwardRef(" + e + ")"), e;
			case ee: return t = e.displayName || null, t === null ? oe(e.type) || "Memo" : t;
			case D:
				t = e._payload, e = e._init;
				try {
					return oe(e(t));
				} catch {}
		}
		return null;
	}
	var se = Array.isArray, O = n.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE, k = r.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE, ce = {
		pending: !1,
		data: null,
		method: null,
		action: null
	}, le = [], ue = -1;
	function de(e) {
		return { current: e };
	}
	function fe(e) {
		0 > ue || (e.current = le[ue], le[ue] = null, ue--);
	}
	function pe(e, t) {
		ue++, le[ue] = e.current, e.current = t;
	}
	var me = de(null), he = de(null), ge = de(null), _e = de(null);
	function ve(e, t) {
		switch (pe(ge, t), pe(he, e), pe(me, null), t.nodeType) {
			case 9:
			case 11:
				e = (e = t.documentElement) && (e = e.namespaceURI) ? Vd(e) : 0;
				break;
			default: if (e = t.tagName, t = t.namespaceURI) t = Vd(t), e = Hd(t, e);
			else switch (e) {
				case "svg":
					e = 1;
					break;
				case "math":
					e = 2;
					break;
				default: e = 0;
			}
		}
		fe(me), pe(me, e);
	}
	function ye() {
		fe(me), fe(he), fe(ge);
	}
	function be(e) {
		e.memoizedState !== null && pe(_e, e);
		var t = me.current, n = Hd(t, e.type);
		t !== n && (pe(he, e), pe(me, n));
	}
	function xe(e) {
		he.current === e && (fe(me), fe(he)), _e.current === e && (fe(_e), Qf._currentValue = ce);
	}
	var Se, Ce;
	function we(e) {
		if (Se === void 0) try {
			throw Error();
		} catch (e) {
			var t = e.stack.trim().match(/\n( *(at )?)/);
			Se = t && t[1] || "", Ce = -1 < e.stack.indexOf("\n    at") ? " (<anonymous>)" : -1 < e.stack.indexOf("@") ? "@unknown:0:0" : "";
		}
		return "\n" + Se + e + Ce;
	}
	var Te = !1;
	function Ee(e, t) {
		if (!e || Te) return "";
		Te = !0;
		var n = Error.prepareStackTrace;
		Error.prepareStackTrace = void 0;
		try {
			var r = { DetermineComponentFrameRoot: function() {
				try {
					if (t) {
						var n = function() {
							throw Error();
						};
						if (Object.defineProperty(n.prototype, "props", { set: function() {
							throw Error();
						} }), typeof Reflect == "object" && Reflect.construct) {
							try {
								Reflect.construct(n, []);
							} catch (e) {
								var r = e;
							}
							Reflect.construct(e, [], n);
						} else {
							try {
								n.call();
							} catch (e) {
								r = e;
							}
							e.call(n.prototype);
						}
					} else {
						try {
							throw Error();
						} catch (e) {
							r = e;
						}
						(n = e()) && typeof n.catch == "function" && n.catch(function() {});
					}
				} catch (e) {
					if (e && r && typeof e.stack == "string") return [e.stack, r.stack];
				}
				return [null, null];
			} };
			r.DetermineComponentFrameRoot.displayName = "DetermineComponentFrameRoot";
			var i = Object.getOwnPropertyDescriptor(r.DetermineComponentFrameRoot, "name");
			i && i.configurable && Object.defineProperty(r.DetermineComponentFrameRoot, "name", { value: "DetermineComponentFrameRoot" });
			var a = r.DetermineComponentFrameRoot(), o = a[0], s = a[1];
			if (o && s) {
				var c = o.split("\n"), l = s.split("\n");
				for (i = r = 0; r < c.length && !c[r].includes("DetermineComponentFrameRoot");) r++;
				for (; i < l.length && !l[i].includes("DetermineComponentFrameRoot");) i++;
				if (r === c.length || i === l.length) for (r = c.length - 1, i = l.length - 1; 1 <= r && 0 <= i && c[r] !== l[i];) i--;
				for (; 1 <= r && 0 <= i; r--, i--) if (c[r] !== l[i]) {
					if (r !== 1 || i !== 1) do
						if (r--, i--, 0 > i || c[r] !== l[i]) {
							var u = "\n" + c[r].replace(" at new ", " at ");
							return e.displayName && u.includes("<anonymous>") && (u = u.replace("<anonymous>", e.displayName)), u;
						}
					while (1 <= r && 0 <= i);
					break;
				}
			}
		} finally {
			Te = !1, Error.prepareStackTrace = n;
		}
		return (n = e ? e.displayName || e.name : "") ? we(n) : "";
	}
	function De(e, t) {
		switch (e.tag) {
			case 26:
			case 27:
			case 5: return we(e.type);
			case 16: return we("Lazy");
			case 13: return e.child !== t && t !== null ? we("Suspense Fallback") : we("Suspense");
			case 19: return we("SuspenseList");
			case 0:
			case 15: return Ee(e.type, !1);
			case 11: return Ee(e.type.render, !1);
			case 1: return Ee(e.type, !0);
			case 31: return we("Activity");
			default: return "";
		}
	}
	function Oe(e) {
		try {
			var t = "", n = null;
			do
				t += De(e, n), n = e, e = e.return;
			while (e);
			return t;
		} catch (e) {
			return "\nError generating stack: " + e.message + "\n" + e.stack;
		}
	}
	var ke = Object.prototype.hasOwnProperty, Ae = t.unstable_scheduleCallback, je = t.unstable_cancelCallback, Me = t.unstable_shouldYield, Ne = t.unstable_requestPaint, Pe = t.unstable_now, Fe = t.unstable_getCurrentPriorityLevel, Ie = t.unstable_ImmediatePriority, Le = t.unstable_UserBlockingPriority, Re = t.unstable_NormalPriority, ze = t.unstable_LowPriority, A = t.unstable_IdlePriority, Be = t.log, Ve = t.unstable_setDisableYieldValue, He = null, Ue = null;
	function We(e) {
		if (typeof Be == "function" && Ve(e), Ue && typeof Ue.setStrictMode == "function") try {
			Ue.setStrictMode(He, e);
		} catch {}
	}
	var Ge = Math.clz32 ? Math.clz32 : Je, Ke = Math.log, qe = Math.LN2;
	function Je(e) {
		return e >>>= 0, e === 0 ? 32 : 31 - (Ke(e) / qe | 0) | 0;
	}
	var Ye = 256, Xe = 262144, Ze = 4194304;
	function Qe(e) {
		var t = e & 42;
		if (t !== 0) return t;
		switch (e & -e) {
			case 1: return 1;
			case 2: return 2;
			case 4: return 4;
			case 8: return 8;
			case 16: return 16;
			case 32: return 32;
			case 64: return 64;
			case 128: return 128;
			case 256:
			case 512:
			case 1024:
			case 2048:
			case 4096:
			case 8192:
			case 16384:
			case 32768:
			case 65536:
			case 131072: return e & 261888;
			case 262144:
			case 524288:
			case 1048576:
			case 2097152: return e & 3932160;
			case 4194304:
			case 8388608:
			case 16777216:
			case 33554432: return e & 62914560;
			case 67108864: return 67108864;
			case 134217728: return 134217728;
			case 268435456: return 268435456;
			case 536870912: return 536870912;
			case 1073741824: return 0;
			default: return e;
		}
	}
	function $e(e, t, n) {
		var r = e.pendingLanes;
		if (r === 0) return 0;
		var i = 0, a = e.suspendedLanes, o = e.pingedLanes;
		e = e.warmLanes;
		var s = r & 134217727;
		return s === 0 ? (s = r & ~a, s === 0 ? o === 0 ? n || (n = r & ~e, n !== 0 && (i = Qe(n))) : i = Qe(o) : i = Qe(s)) : (r = s & ~a, r === 0 ? (o &= s, o === 0 ? n || (n = s & ~e, n !== 0 && (i = Qe(n))) : i = Qe(o)) : i = Qe(r)), i === 0 ? 0 : t !== 0 && t !== i && (t & a) === 0 && (a = i & -i, n = t & -t, a >= n || a === 32 && n & 4194048) ? t : i;
	}
	function et(e, t) {
		return (e.pendingLanes & ~(e.suspendedLanes & ~e.pingedLanes) & t) === 0;
	}
	function tt(e, t) {
		switch (e) {
			case 1:
			case 2:
			case 4:
			case 8:
			case 64: return t + 250;
			case 16:
			case 32:
			case 128:
			case 256:
			case 512:
			case 1024:
			case 2048:
			case 4096:
			case 8192:
			case 16384:
			case 32768:
			case 65536:
			case 131072:
			case 262144:
			case 524288:
			case 1048576:
			case 2097152: return t + 5e3;
			case 4194304:
			case 8388608:
			case 16777216:
			case 33554432: return -1;
			case 67108864:
			case 134217728:
			case 268435456:
			case 536870912:
			case 1073741824: return -1;
			default: return -1;
		}
	}
	function nt() {
		var e = Ze;
		return Ze <<= 1, !(Ze & 62914560) && (Ze = 4194304), e;
	}
	function rt(e) {
		for (var t = [], n = 0; 31 > n; n++) t.push(e);
		return t;
	}
	function it(e, t) {
		e.pendingLanes |= t, t !== 268435456 && (e.suspendedLanes = 0, e.pingedLanes = 0, e.warmLanes = 0);
	}
	function at(e, t, n, r, i, a) {
		var o = e.pendingLanes;
		e.pendingLanes = n, e.suspendedLanes = 0, e.pingedLanes = 0, e.warmLanes = 0, e.expiredLanes &= n, e.entangledLanes &= n, e.errorRecoveryDisabledLanes &= n, e.shellSuspendCounter = 0;
		var s = e.entanglements, c = e.expirationTimes, l = e.hiddenUpdates;
		for (n = o & ~n; 0 < n;) {
			var u = 31 - Ge(n), d = 1 << u;
			s[u] = 0, c[u] = -1;
			var f = l[u];
			if (f !== null) for (l[u] = null, u = 0; u < f.length; u++) {
				var p = f[u];
				p !== null && (p.lane &= -536870913);
			}
			n &= ~d;
		}
		r !== 0 && ot(e, r, 0), a !== 0 && i === 0 && e.tag !== 0 && (e.suspendedLanes |= a & ~(o & ~t));
	}
	function ot(e, t, n) {
		e.pendingLanes |= t, e.suspendedLanes &= ~t;
		var r = 31 - Ge(t);
		e.entangledLanes |= t, e.entanglements[r] = e.entanglements[r] | 1073741824 | n & 261930;
	}
	function st(e, t) {
		var n = e.entangledLanes |= t;
		for (e = e.entanglements; n;) {
			var r = 31 - Ge(n), i = 1 << r;
			i & t | e[r] & t && (e[r] |= t), n &= ~i;
		}
	}
	function ct(e, t) {
		var n = t & -t;
		return n = n & 42 ? 1 : lt(n), (n & (e.suspendedLanes | t)) === 0 ? n : 0;
	}
	function lt(e) {
		switch (e) {
			case 2:
				e = 1;
				break;
			case 8:
				e = 4;
				break;
			case 32:
				e = 16;
				break;
			case 256:
			case 512:
			case 1024:
			case 2048:
			case 4096:
			case 8192:
			case 16384:
			case 32768:
			case 65536:
			case 131072:
			case 262144:
			case 524288:
			case 1048576:
			case 2097152:
			case 4194304:
			case 8388608:
			case 16777216:
			case 33554432:
				e = 128;
				break;
			case 268435456:
				e = 134217728;
				break;
			default: e = 0;
		}
		return e;
	}
	function ut(e) {
		return e &= -e, 2 < e ? 8 < e ? e & 134217727 ? 32 : 268435456 : 8 : 2;
	}
	function dt() {
		var e = k.p;
		return e === 0 ? (e = window.event, e === void 0 ? 32 : mp(e.type)) : e;
	}
	function ft(e, t) {
		var n = k.p;
		try {
			return k.p = e, t();
		} finally {
			k.p = n;
		}
	}
	var pt = Math.random().toString(36).slice(2), mt = "__reactFiber$" + pt, ht = "__reactProps$" + pt, gt = "__reactContainer$" + pt, _t = "__reactEvents$" + pt, vt = "__reactListeners$" + pt, yt = "__reactHandles$" + pt, bt = "__reactResources$" + pt, xt = "__reactMarker$" + pt;
	function St(e) {
		delete e[mt], delete e[ht], delete e[_t], delete e[vt], delete e[yt];
	}
	function Ct(e) {
		var t = e[mt];
		if (t) return t;
		for (var n = e.parentNode; n;) {
			if (t = n[gt] || n[mt]) {
				if (n = t.alternate, t.child !== null || n !== null && n.child !== null) for (e = df(e); e !== null;) {
					if (n = e[mt]) return n;
					e = df(e);
				}
				return t;
			}
			e = n, n = e.parentNode;
		}
		return null;
	}
	function wt(e) {
		if (e = e[mt] || e[gt]) {
			var t = e.tag;
			if (t === 5 || t === 6 || t === 13 || t === 31 || t === 26 || t === 27 || t === 3) return e;
		}
		return null;
	}
	function Tt(e) {
		var t = e.tag;
		if (t === 5 || t === 26 || t === 27 || t === 6) return e.stateNode;
		throw Error(i(33));
	}
	function Et(e) {
		var t = e[bt];
		return t ||= e[bt] = {
			hoistableStyles: /* @__PURE__ */ new Map(),
			hoistableScripts: /* @__PURE__ */ new Map()
		}, t;
	}
	function Dt(e) {
		e[xt] = !0;
	}
	var Ot = /* @__PURE__ */ new Set(), kt = {};
	function At(e, t) {
		jt(e, t), jt(e + "Capture", t);
	}
	function jt(e, t) {
		for (kt[e] = t, e = 0; e < t.length; e++) Ot.add(t[e]);
	}
	var Mt = RegExp("^[:A-Z_a-z\\u00C0-\\u00D6\\u00D8-\\u00F6\\u00F8-\\u02FF\\u0370-\\u037D\\u037F-\\u1FFF\\u200C-\\u200D\\u2070-\\u218F\\u2C00-\\u2FEF\\u3001-\\uD7FF\\uF900-\\uFDCF\\uFDF0-\\uFFFD][:A-Z_a-z\\u00C0-\\u00D6\\u00D8-\\u00F6\\u00F8-\\u02FF\\u0370-\\u037D\\u037F-\\u1FFF\\u200C-\\u200D\\u2070-\\u218F\\u2C00-\\u2FEF\\u3001-\\uD7FF\\uF900-\\uFDCF\\uFDF0-\\uFFFD\\-.0-9\\u00B7\\u0300-\\u036F\\u203F-\\u2040]*$"), Nt = {}, Pt = {};
	function Ft(e) {
		return ke.call(Pt, e) ? !0 : ke.call(Nt, e) ? !1 : Mt.test(e) ? Pt[e] = !0 : (Nt[e] = !0, !1);
	}
	function It(e, t, n) {
		if (Ft(t)) if (n === null) e.removeAttribute(t);
		else {
			switch (typeof n) {
				case "undefined":
				case "function":
				case "symbol":
					e.removeAttribute(t);
					return;
				case "boolean":
					var r = t.toLowerCase().slice(0, 5);
					if (r !== "data-" && r !== "aria-") {
						e.removeAttribute(t);
						return;
					}
			}
			e.setAttribute(t, "" + n);
		}
	}
	function Lt(e, t, n) {
		if (n === null) e.removeAttribute(t);
		else {
			switch (typeof n) {
				case "undefined":
				case "function":
				case "symbol":
				case "boolean":
					e.removeAttribute(t);
					return;
			}
			e.setAttribute(t, "" + n);
		}
	}
	function Rt(e, t, n, r) {
		if (r === null) e.removeAttribute(n);
		else {
			switch (typeof r) {
				case "undefined":
				case "function":
				case "symbol":
				case "boolean":
					e.removeAttribute(n);
					return;
			}
			e.setAttributeNS(t, n, "" + r);
		}
	}
	function zt(e) {
		switch (typeof e) {
			case "bigint":
			case "boolean":
			case "number":
			case "string":
			case "undefined": return e;
			case "object": return e;
			default: return "";
		}
	}
	function Bt(e) {
		var t = e.type;
		return (e = e.nodeName) && e.toLowerCase() === "input" && (t === "checkbox" || t === "radio");
	}
	function j(e, t, n) {
		var r = Object.getOwnPropertyDescriptor(e.constructor.prototype, t);
		if (!e.hasOwnProperty(t) && r !== void 0 && typeof r.get == "function" && typeof r.set == "function") {
			var i = r.get, a = r.set;
			return Object.defineProperty(e, t, {
				configurable: !0,
				get: function() {
					return i.call(this);
				},
				set: function(e) {
					n = "" + e, a.call(this, e);
				}
			}), Object.defineProperty(e, t, { enumerable: r.enumerable }), {
				getValue: function() {
					return n;
				},
				setValue: function(e) {
					n = "" + e;
				},
				stopTracking: function() {
					e._valueTracker = null, delete e[t];
				}
			};
		}
	}
	function Vt(e) {
		if (!e._valueTracker) {
			var t = Bt(e) ? "checked" : "value";
			e._valueTracker = j(e, t, "" + e[t]);
		}
	}
	function Ht(e) {
		if (!e) return !1;
		var t = e._valueTracker;
		if (!t) return !0;
		var n = t.getValue(), r = "";
		return e && (r = Bt(e) ? e.checked ? "true" : "false" : e.value), e = r, e === n ? !1 : (t.setValue(e), !0);
	}
	function M(e) {
		if (e ||= typeof document < "u" ? document : void 0, e === void 0) return null;
		try {
			return e.activeElement || e.body;
		} catch {
			return e.body;
		}
	}
	var Ut = /[\n"\\]/g;
	function Wt(e) {
		return e.replace(Ut, function(e) {
			return "\\" + e.charCodeAt(0).toString(16) + " ";
		});
	}
	function Gt(e, t, n, r, i, a, o, s) {
		e.name = "", o != null && typeof o != "function" && typeof o != "symbol" && typeof o != "boolean" ? e.type = o : e.removeAttribute("type"), t == null ? o !== "submit" && o !== "reset" || e.removeAttribute("value") : o === "number" ? (t === 0 && e.value === "" || e.value != t) && (e.value = "" + zt(t)) : e.value !== "" + zt(t) && (e.value = "" + zt(t)), t == null ? n == null ? r != null && e.removeAttribute("value") : qt(e, o, zt(n)) : qt(e, o, zt(t)), i == null && a != null && (e.defaultChecked = !!a), i != null && (e.checked = i && typeof i != "function" && typeof i != "symbol"), s != null && typeof s != "function" && typeof s != "symbol" && typeof s != "boolean" ? e.name = "" + zt(s) : e.removeAttribute("name");
	}
	function Kt(e, t, n, r, i, a, o, s) {
		if (a != null && typeof a != "function" && typeof a != "symbol" && typeof a != "boolean" && (e.type = a), t != null || n != null) {
			if (!(a !== "submit" && a !== "reset" || t != null)) {
				Vt(e);
				return;
			}
			n = n == null ? "" : "" + zt(n), t = t == null ? n : "" + zt(t), s || t === e.value || (e.value = t), e.defaultValue = t;
		}
		r ??= i, r = typeof r != "function" && typeof r != "symbol" && !!r, e.checked = s ? e.checked : !!r, e.defaultChecked = !!r, o != null && typeof o != "function" && typeof o != "symbol" && typeof o != "boolean" && (e.name = o), Vt(e);
	}
	function qt(e, t, n) {
		t === "number" && M(e.ownerDocument) === e || e.defaultValue === "" + n || (e.defaultValue = "" + n);
	}
	function Jt(e, t, n, r) {
		if (e = e.options, t) {
			t = {};
			for (var i = 0; i < n.length; i++) t["$" + n[i]] = !0;
			for (n = 0; n < e.length; n++) i = t.hasOwnProperty("$" + e[n].value), e[n].selected !== i && (e[n].selected = i), i && r && (e[n].defaultSelected = !0);
		} else {
			for (n = "" + zt(n), t = null, i = 0; i < e.length; i++) {
				if (e[i].value === n) {
					e[i].selected = !0, r && (e[i].defaultSelected = !0);
					return;
				}
				t !== null || e[i].disabled || (t = e[i]);
			}
			t !== null && (t.selected = !0);
		}
	}
	function Yt(e, t, n) {
		if (t != null && (t = "" + zt(t), t !== e.value && (e.value = t), n == null)) {
			e.defaultValue !== t && (e.defaultValue = t);
			return;
		}
		e.defaultValue = n == null ? "" : "" + zt(n);
	}
	function Xt(e, t, n, r) {
		if (t == null) {
			if (r != null) {
				if (n != null) throw Error(i(92));
				if (se(r)) {
					if (1 < r.length) throw Error(i(93));
					r = r[0];
				}
				n = r;
			}
			n ??= "", t = n;
		}
		n = zt(t), e.defaultValue = n, r = e.textContent, r === n && r !== "" && r !== null && (e.value = r), Vt(e);
	}
	function Zt(e, t) {
		if (t) {
			var n = e.firstChild;
			if (n && n === e.lastChild && n.nodeType === 3) {
				n.nodeValue = t;
				return;
			}
		}
		e.textContent = t;
	}
	var Qt = new Set("animationIterationCount aspectRatio borderImageOutset borderImageSlice borderImageWidth boxFlex boxFlexGroup boxOrdinalGroup columnCount columns flex flexGrow flexPositive flexShrink flexNegative flexOrder gridArea gridRow gridRowEnd gridRowSpan gridRowStart gridColumn gridColumnEnd gridColumnSpan gridColumnStart fontWeight lineClamp lineHeight opacity order orphans scale tabSize widows zIndex zoom fillOpacity floodOpacity stopOpacity strokeDasharray strokeDashoffset strokeMiterlimit strokeOpacity strokeWidth MozAnimationIterationCount MozBoxFlex MozBoxFlexGroup MozLineClamp msAnimationIterationCount msFlex msZoom msFlexGrow msFlexNegative msFlexOrder msFlexPositive msFlexShrink msGridColumn msGridColumnSpan msGridRow msGridRowSpan WebkitAnimationIterationCount WebkitBoxFlex WebKitBoxFlexGroup WebkitBoxOrdinalGroup WebkitColumnCount WebkitColumns WebkitFlex WebkitFlexGrow WebkitFlexPositive WebkitFlexShrink WebkitLineClamp".split(" "));
	function $t(e, t, n) {
		var r = t.indexOf("--") === 0;
		n == null || typeof n == "boolean" || n === "" ? r ? e.setProperty(t, "") : t === "float" ? e.cssFloat = "" : e[t] = "" : r ? e.setProperty(t, n) : typeof n != "number" || n === 0 || Qt.has(t) ? t === "float" ? e.cssFloat = n : e[t] = ("" + n).trim() : e[t] = n + "px";
	}
	function en(e, t, n) {
		if (t != null && typeof t != "object") throw Error(i(62));
		if (e = e.style, n != null) {
			for (var r in n) !n.hasOwnProperty(r) || t != null && t.hasOwnProperty(r) || (r.indexOf("--") === 0 ? e.setProperty(r, "") : r === "float" ? e.cssFloat = "" : e[r] = "");
			for (var a in t) r = t[a], t.hasOwnProperty(a) && n[a] !== r && $t(e, a, r);
		} else for (var o in t) t.hasOwnProperty(o) && $t(e, o, t[o]);
	}
	function tn(e) {
		if (e.indexOf("-") === -1) return !1;
		switch (e) {
			case "annotation-xml":
			case "color-profile":
			case "font-face":
			case "font-face-src":
			case "font-face-uri":
			case "font-face-format":
			case "font-face-name":
			case "missing-glyph": return !1;
			default: return !0;
		}
	}
	var nn = new Map([
		["acceptCharset", "accept-charset"],
		["htmlFor", "for"],
		["httpEquiv", "http-equiv"],
		["crossOrigin", "crossorigin"],
		["accentHeight", "accent-height"],
		["alignmentBaseline", "alignment-baseline"],
		["arabicForm", "arabic-form"],
		["baselineShift", "baseline-shift"],
		["capHeight", "cap-height"],
		["clipPath", "clip-path"],
		["clipRule", "clip-rule"],
		["colorInterpolation", "color-interpolation"],
		["colorInterpolationFilters", "color-interpolation-filters"],
		["colorProfile", "color-profile"],
		["colorRendering", "color-rendering"],
		["dominantBaseline", "dominant-baseline"],
		["enableBackground", "enable-background"],
		["fillOpacity", "fill-opacity"],
		["fillRule", "fill-rule"],
		["floodColor", "flood-color"],
		["floodOpacity", "flood-opacity"],
		["fontFamily", "font-family"],
		["fontSize", "font-size"],
		["fontSizeAdjust", "font-size-adjust"],
		["fontStretch", "font-stretch"],
		["fontStyle", "font-style"],
		["fontVariant", "font-variant"],
		["fontWeight", "font-weight"],
		["glyphName", "glyph-name"],
		["glyphOrientationHorizontal", "glyph-orientation-horizontal"],
		["glyphOrientationVertical", "glyph-orientation-vertical"],
		["horizAdvX", "horiz-adv-x"],
		["horizOriginX", "horiz-origin-x"],
		["imageRendering", "image-rendering"],
		["letterSpacing", "letter-spacing"],
		["lightingColor", "lighting-color"],
		["markerEnd", "marker-end"],
		["markerMid", "marker-mid"],
		["markerStart", "marker-start"],
		["overlinePosition", "overline-position"],
		["overlineThickness", "overline-thickness"],
		["paintOrder", "paint-order"],
		["panose-1", "panose-1"],
		["pointerEvents", "pointer-events"],
		["renderingIntent", "rendering-intent"],
		["shapeRendering", "shape-rendering"],
		["stopColor", "stop-color"],
		["stopOpacity", "stop-opacity"],
		["strikethroughPosition", "strikethrough-position"],
		["strikethroughThickness", "strikethrough-thickness"],
		["strokeDasharray", "stroke-dasharray"],
		["strokeDashoffset", "stroke-dashoffset"],
		["strokeLinecap", "stroke-linecap"],
		["strokeLinejoin", "stroke-linejoin"],
		["strokeMiterlimit", "stroke-miterlimit"],
		["strokeOpacity", "stroke-opacity"],
		["strokeWidth", "stroke-width"],
		["textAnchor", "text-anchor"],
		["textDecoration", "text-decoration"],
		["textRendering", "text-rendering"],
		["transformOrigin", "transform-origin"],
		["underlinePosition", "underline-position"],
		["underlineThickness", "underline-thickness"],
		["unicodeBidi", "unicode-bidi"],
		["unicodeRange", "unicode-range"],
		["unitsPerEm", "units-per-em"],
		["vAlphabetic", "v-alphabetic"],
		["vHanging", "v-hanging"],
		["vIdeographic", "v-ideographic"],
		["vMathematical", "v-mathematical"],
		["vectorEffect", "vector-effect"],
		["vertAdvY", "vert-adv-y"],
		["vertOriginX", "vert-origin-x"],
		["vertOriginY", "vert-origin-y"],
		["wordSpacing", "word-spacing"],
		["writingMode", "writing-mode"],
		["xmlnsXlink", "xmlns:xlink"],
		["xHeight", "x-height"]
	]), rn = /^[\u0000-\u001F ]*j[\r\n\t]*a[\r\n\t]*v[\r\n\t]*a[\r\n\t]*s[\r\n\t]*c[\r\n\t]*r[\r\n\t]*i[\r\n\t]*p[\r\n\t]*t[\r\n\t]*:/i;
	function an(e) {
		return rn.test("" + e) ? "javascript:throw new Error('React has blocked a javascript: URL as a security precaution.')" : e;
	}
	function on() {}
	var sn = null;
	function cn(e) {
		return e = e.target || e.srcElement || window, e.correspondingUseElement && (e = e.correspondingUseElement), e.nodeType === 3 ? e.parentNode : e;
	}
	var ln = null, un = null;
	function dn(e) {
		var t = wt(e);
		if (t && (e = t.stateNode)) {
			var n = e[ht] || null;
			a: switch (e = t.stateNode, t.type) {
				case "input":
					if (Gt(e, n.value, n.defaultValue, n.defaultValue, n.checked, n.defaultChecked, n.type, n.name), t = n.name, n.type === "radio" && t != null) {
						for (n = e; n.parentNode;) n = n.parentNode;
						for (n = n.querySelectorAll("input[name=\"" + Wt("" + t) + "\"][type=\"radio\"]"), t = 0; t < n.length; t++) {
							var r = n[t];
							if (r !== e && r.form === e.form) {
								var a = r[ht] || null;
								if (!a) throw Error(i(90));
								Gt(r, a.value, a.defaultValue, a.defaultValue, a.checked, a.defaultChecked, a.type, a.name);
							}
						}
						for (t = 0; t < n.length; t++) r = n[t], r.form === e.form && Ht(r);
					}
					break a;
				case "textarea":
					Yt(e, n.value, n.defaultValue);
					break a;
				case "select": t = n.value, t != null && Jt(e, !!n.multiple, t, !1);
			}
		}
	}
	var fn = !1;
	function pn(e, t, n) {
		if (fn) return e(t, n);
		fn = !0;
		try {
			return e(t);
		} finally {
			if (fn = !1, (ln !== null || un !== null) && (vu(), ln && (t = ln, e = un, un = ln = null, dn(t), e))) for (t = 0; t < e.length; t++) dn(e[t]);
		}
	}
	function mn(e, t) {
		var n = e.stateNode;
		if (n === null) return null;
		var r = n[ht] || null;
		if (r === null) return null;
		n = r[t];
		a: switch (t) {
			case "onClick":
			case "onClickCapture":
			case "onDoubleClick":
			case "onDoubleClickCapture":
			case "onMouseDown":
			case "onMouseDownCapture":
			case "onMouseMove":
			case "onMouseMoveCapture":
			case "onMouseUp":
			case "onMouseUpCapture":
			case "onMouseEnter":
				(r = !r.disabled) || (e = e.type, r = !(e === "button" || e === "input" || e === "select" || e === "textarea")), e = !r;
				break a;
			default: e = !1;
		}
		if (e) return null;
		if (n && typeof n != "function") throw Error(i(231, t, typeof n));
		return n;
	}
	var hn = !(typeof window > "u" || window.document === void 0 || window.document.createElement === void 0), gn = !1;
	if (hn) try {
		var _n = {};
		Object.defineProperty(_n, "passive", { get: function() {
			gn = !0;
		} }), window.addEventListener("test", _n, _n), window.removeEventListener("test", _n, _n);
	} catch {
		gn = !1;
	}
	var vn = null, yn = null, bn = null;
	function xn() {
		if (bn) return bn;
		var e, t = yn, n = t.length, r, i = "value" in vn ? vn.value : vn.textContent, a = i.length;
		for (e = 0; e < n && t[e] === i[e]; e++);
		var o = n - e;
		for (r = 1; r <= o && t[n - r] === i[a - r]; r++);
		return bn = i.slice(e, 1 < r ? 1 - r : void 0);
	}
	function Sn(e) {
		var t = e.keyCode;
		return "charCode" in e ? (e = e.charCode, e === 0 && t === 13 && (e = 13)) : e = t, e === 10 && (e = 13), 32 <= e || e === 13 ? e : 0;
	}
	function Cn() {
		return !0;
	}
	function wn() {
		return !1;
	}
	function Tn(e) {
		function t(t, n, r, i, a) {
			for (var o in this._reactName = t, this._targetInst = r, this.type = n, this.nativeEvent = i, this.target = a, this.currentTarget = null, e) e.hasOwnProperty(o) && (t = e[o], this[o] = t ? t(i) : i[o]);
			return this.isDefaultPrevented = (i.defaultPrevented == null ? !1 === i.returnValue : i.defaultPrevented) ? Cn : wn, this.isPropagationStopped = wn, this;
		}
		return m(t.prototype, {
			preventDefault: function() {
				this.defaultPrevented = !0;
				var e = this.nativeEvent;
				e && (e.preventDefault ? e.preventDefault() : typeof e.returnValue != "unknown" && (e.returnValue = !1), this.isDefaultPrevented = Cn);
			},
			stopPropagation: function() {
				var e = this.nativeEvent;
				e && (e.stopPropagation ? e.stopPropagation() : typeof e.cancelBubble != "unknown" && (e.cancelBubble = !0), this.isPropagationStopped = Cn);
			},
			persist: function() {},
			isPersistent: Cn
		}), t;
	}
	var En = {
		eventPhase: 0,
		bubbles: 0,
		cancelable: 0,
		timeStamp: function(e) {
			return e.timeStamp || Date.now();
		},
		defaultPrevented: 0,
		isTrusted: 0
	}, Dn = Tn(En), On = m({}, En, {
		view: 0,
		detail: 0
	}), kn = Tn(On), An, jn, Mn, Nn = m({}, On, {
		screenX: 0,
		screenY: 0,
		clientX: 0,
		clientY: 0,
		pageX: 0,
		pageY: 0,
		ctrlKey: 0,
		shiftKey: 0,
		altKey: 0,
		metaKey: 0,
		getModifierState: N,
		button: 0,
		buttons: 0,
		relatedTarget: function(e) {
			return e.relatedTarget === void 0 ? e.fromElement === e.srcElement ? e.toElement : e.fromElement : e.relatedTarget;
		},
		movementX: function(e) {
			return "movementX" in e ? e.movementX : (e !== Mn && (Mn && e.type === "mousemove" ? (An = e.screenX - Mn.screenX, jn = e.screenY - Mn.screenY) : jn = An = 0, Mn = e), An);
		},
		movementY: function(e) {
			return "movementY" in e ? e.movementY : jn;
		}
	}), Pn = Tn(Nn), Fn = Tn(m({}, Nn, { dataTransfer: 0 })), In = Tn(m({}, On, { relatedTarget: 0 })), Ln = Tn(m({}, En, {
		animationName: 0,
		elapsedTime: 0,
		pseudoElement: 0
	})), Rn = Tn(m({}, En, { clipboardData: function(e) {
		return "clipboardData" in e ? e.clipboardData : window.clipboardData;
	} })), zn = Tn(m({}, En, { data: 0 })), Bn = {
		Esc: "Escape",
		Spacebar: " ",
		Left: "ArrowLeft",
		Up: "ArrowUp",
		Right: "ArrowRight",
		Down: "ArrowDown",
		Del: "Delete",
		Win: "OS",
		Menu: "ContextMenu",
		Apps: "ContextMenu",
		Scroll: "ScrollLock",
		MozPrintableKey: "Unidentified"
	}, Vn = {
		8: "Backspace",
		9: "Tab",
		12: "Clear",
		13: "Enter",
		16: "Shift",
		17: "Control",
		18: "Alt",
		19: "Pause",
		20: "CapsLock",
		27: "Escape",
		32: " ",
		33: "PageUp",
		34: "PageDown",
		35: "End",
		36: "Home",
		37: "ArrowLeft",
		38: "ArrowUp",
		39: "ArrowRight",
		40: "ArrowDown",
		45: "Insert",
		46: "Delete",
		112: "F1",
		113: "F2",
		114: "F3",
		115: "F4",
		116: "F5",
		117: "F6",
		118: "F7",
		119: "F8",
		120: "F9",
		121: "F10",
		122: "F11",
		123: "F12",
		144: "NumLock",
		145: "ScrollLock",
		224: "Meta"
	}, Hn = {
		Alt: "altKey",
		Control: "ctrlKey",
		Meta: "metaKey",
		Shift: "shiftKey"
	};
	function Un(e) {
		var t = this.nativeEvent;
		return t.getModifierState ? t.getModifierState(e) : (e = Hn[e]) ? !!t[e] : !1;
	}
	function N() {
		return Un;
	}
	var Wn = Tn(m({}, On, {
		key: function(e) {
			if (e.key) {
				var t = Bn[e.key] || e.key;
				if (t !== "Unidentified") return t;
			}
			return e.type === "keypress" ? (e = Sn(e), e === 13 ? "Enter" : String.fromCharCode(e)) : e.type === "keydown" || e.type === "keyup" ? Vn[e.keyCode] || "Unidentified" : "";
		},
		code: 0,
		location: 0,
		ctrlKey: 0,
		shiftKey: 0,
		altKey: 0,
		metaKey: 0,
		repeat: 0,
		locale: 0,
		getModifierState: N,
		charCode: function(e) {
			return e.type === "keypress" ? Sn(e) : 0;
		},
		keyCode: function(e) {
			return e.type === "keydown" || e.type === "keyup" ? e.keyCode : 0;
		},
		which: function(e) {
			return e.type === "keypress" ? Sn(e) : e.type === "keydown" || e.type === "keyup" ? e.keyCode : 0;
		}
	})), Gn = Tn(m({}, Nn, {
		pointerId: 0,
		width: 0,
		height: 0,
		pressure: 0,
		tangentialPressure: 0,
		tiltX: 0,
		tiltY: 0,
		twist: 0,
		pointerType: 0,
		isPrimary: 0
	})), Kn = Tn(m({}, On, {
		touches: 0,
		targetTouches: 0,
		changedTouches: 0,
		altKey: 0,
		metaKey: 0,
		ctrlKey: 0,
		shiftKey: 0,
		getModifierState: N
	})), qn = Tn(m({}, En, {
		propertyName: 0,
		elapsedTime: 0,
		pseudoElement: 0
	})), Jn = Tn(m({}, Nn, {
		deltaX: function(e) {
			return "deltaX" in e ? e.deltaX : "wheelDeltaX" in e ? -e.wheelDeltaX : 0;
		},
		deltaY: function(e) {
			return "deltaY" in e ? e.deltaY : "wheelDeltaY" in e ? -e.wheelDeltaY : "wheelDelta" in e ? -e.wheelDelta : 0;
		},
		deltaZ: 0,
		deltaMode: 0
	})), Yn = Tn(m({}, En, {
		newState: 0,
		oldState: 0
	})), Xn = [
		9,
		13,
		27,
		32
	], Zn = hn && "CompositionEvent" in window, Qn = null;
	hn && "documentMode" in document && (Qn = document.documentMode);
	var $n = hn && "TextEvent" in window && !Qn, er = hn && (!Zn || Qn && 8 < Qn && 11 >= Qn), tr = " ", nr = !1;
	function rr(e, t) {
		switch (e) {
			case "keyup": return Xn.indexOf(t.keyCode) !== -1;
			case "keydown": return t.keyCode !== 229;
			case "keypress":
			case "mousedown":
			case "focusout": return !0;
			default: return !1;
		}
	}
	function ir(e) {
		return e = e.detail, typeof e == "object" && "data" in e ? e.data : null;
	}
	var ar = !1;
	function or(e, t) {
		switch (e) {
			case "compositionend": return ir(t);
			case "keypress": return t.which === 32 ? (nr = !0, tr) : null;
			case "textInput": return e = t.data, e === tr && nr ? null : e;
			default: return null;
		}
	}
	function sr(e, t) {
		if (ar) return e === "compositionend" || !Zn && rr(e, t) ? (e = xn(), bn = yn = vn = null, ar = !1, e) : null;
		switch (e) {
			case "paste": return null;
			case "keypress":
				if (!(t.ctrlKey || t.altKey || t.metaKey) || t.ctrlKey && t.altKey) {
					if (t.char && 1 < t.char.length) return t.char;
					if (t.which) return String.fromCharCode(t.which);
				}
				return null;
			case "compositionend": return er && t.locale !== "ko" ? null : t.data;
			default: return null;
		}
	}
	var cr = {
		color: !0,
		date: !0,
		datetime: !0,
		"datetime-local": !0,
		email: !0,
		month: !0,
		number: !0,
		password: !0,
		range: !0,
		search: !0,
		tel: !0,
		text: !0,
		time: !0,
		url: !0,
		week: !0
	};
	function lr(e) {
		var t = e && e.nodeName && e.nodeName.toLowerCase();
		return t === "input" ? !!cr[e.type] : t === "textarea";
	}
	function ur(e, t, n, r) {
		ln ? un ? un.push(r) : un = [r] : ln = r, t = Td(t, "onChange"), 0 < t.length && (n = new Dn("onChange", "change", null, n, r), e.push({
			event: n,
			listeners: t
		}));
	}
	var dr = null, fr = null;
	function pr(e) {
		_d(e, 0);
	}
	function mr(e) {
		if (Ht(Tt(e))) return e;
	}
	function hr(e, t) {
		if (e === "change") return t;
	}
	var gr = !1;
	if (hn) {
		var _r;
		if (hn) {
			var vr = "oninput" in document;
			if (!vr) {
				var yr = document.createElement("div");
				yr.setAttribute("oninput", "return;"), vr = typeof yr.oninput == "function";
			}
			_r = vr;
		} else _r = !1;
		gr = _r && (!document.documentMode || 9 < document.documentMode);
	}
	function P() {
		dr && (dr.detachEvent("onpropertychange", br), fr = dr = null);
	}
	function br(e) {
		if (e.propertyName === "value" && mr(fr)) {
			var t = [];
			ur(t, fr, e, cn(e)), pn(pr, t);
		}
	}
	function xr(e, t, n) {
		e === "focusin" ? (P(), dr = t, fr = n, dr.attachEvent("onpropertychange", br)) : e === "focusout" && P();
	}
	function Sr(e) {
		if (e === "selectionchange" || e === "keyup" || e === "keydown") return mr(fr);
	}
	function Cr(e, t) {
		if (e === "click") return mr(t);
	}
	function wr(e, t) {
		if (e === "input" || e === "change") return mr(t);
	}
	function Tr(e, t) {
		return e === t && (e !== 0 || 1 / e == 1 / t) || e !== e && t !== t;
	}
	var Er = typeof Object.is == "function" ? Object.is : Tr;
	function Dr(e, t) {
		if (Er(e, t)) return !0;
		if (typeof e != "object" || !e || typeof t != "object" || !t) return !1;
		var n = Object.keys(e), r = Object.keys(t);
		if (n.length !== r.length) return !1;
		for (r = 0; r < n.length; r++) {
			var i = n[r];
			if (!ke.call(t, i) || !Er(e[i], t[i])) return !1;
		}
		return !0;
	}
	function Or(e) {
		for (; e && e.firstChild;) e = e.firstChild;
		return e;
	}
	function kr(e, t) {
		var n = Or(e);
		e = 0;
		for (var r; n;) {
			if (n.nodeType === 3) {
				if (r = e + n.textContent.length, e <= t && r >= t) return {
					node: n,
					offset: t - e
				};
				e = r;
			}
			a: {
				for (; n;) {
					if (n.nextSibling) {
						n = n.nextSibling;
						break a;
					}
					n = n.parentNode;
				}
				n = void 0;
			}
			n = Or(n);
		}
	}
	function Ar(e, t) {
		return e && t ? e === t ? !0 : e && e.nodeType === 3 ? !1 : t && t.nodeType === 3 ? Ar(e, t.parentNode) : "contains" in e ? e.contains(t) : e.compareDocumentPosition ? !!(e.compareDocumentPosition(t) & 16) : !1 : !1;
	}
	function jr(e) {
		e = e != null && e.ownerDocument != null && e.ownerDocument.defaultView != null ? e.ownerDocument.defaultView : window;
		for (var t = M(e.document); t instanceof e.HTMLIFrameElement;) {
			try {
				var n = typeof t.contentWindow.location.href == "string";
			} catch {
				n = !1;
			}
			if (n) e = t.contentWindow;
			else break;
			t = M(e.document);
		}
		return t;
	}
	function Mr(e) {
		var t = e && e.nodeName && e.nodeName.toLowerCase();
		return t && (t === "input" && (e.type === "text" || e.type === "search" || e.type === "tel" || e.type === "url" || e.type === "password") || t === "textarea" || e.contentEditable === "true");
	}
	var Nr = hn && "documentMode" in document && 11 >= document.documentMode, Pr = null, Fr = null, Ir = null, Lr = !1;
	function Rr(e, t, n) {
		var r = n.window === n ? n.document : n.nodeType === 9 ? n : n.ownerDocument;
		Lr || Pr == null || Pr !== M(r) || (r = Pr, "selectionStart" in r && Mr(r) ? r = {
			start: r.selectionStart,
			end: r.selectionEnd
		} : (r = (r.ownerDocument && r.ownerDocument.defaultView || window).getSelection(), r = {
			anchorNode: r.anchorNode,
			anchorOffset: r.anchorOffset,
			focusNode: r.focusNode,
			focusOffset: r.focusOffset
		}), Ir && Dr(Ir, r) || (Ir = r, r = Td(Fr, "onSelect"), 0 < r.length && (t = new Dn("onSelect", "select", null, t, n), e.push({
			event: t,
			listeners: r
		}), t.target = Pr)));
	}
	function zr(e, t) {
		var n = {};
		return n[e.toLowerCase()] = t.toLowerCase(), n["Webkit" + e] = "webkit" + t, n["Moz" + e] = "moz" + t, n;
	}
	var Br = {
		animationend: zr("Animation", "AnimationEnd"),
		animationiteration: zr("Animation", "AnimationIteration"),
		animationstart: zr("Animation", "AnimationStart"),
		transitionrun: zr("Transition", "TransitionRun"),
		transitionstart: zr("Transition", "TransitionStart"),
		transitioncancel: zr("Transition", "TransitionCancel"),
		transitionend: zr("Transition", "TransitionEnd")
	}, Vr = {}, Hr = {};
	hn && (Hr = document.createElement("div").style, "AnimationEvent" in window || (delete Br.animationend.animation, delete Br.animationiteration.animation, delete Br.animationstart.animation), "TransitionEvent" in window || delete Br.transitionend.transition);
	function F(e) {
		if (Vr[e]) return Vr[e];
		if (!Br[e]) return e;
		var t = Br[e], n;
		for (n in t) if (t.hasOwnProperty(n) && n in Hr) return Vr[e] = t[n];
		return e;
	}
	var Ur = F("animationend"), Wr = F("animationiteration"), Gr = F("animationstart"), Kr = F("transitionrun"), qr = F("transitionstart"), Jr = F("transitioncancel"), Yr = F("transitionend"), Xr = /* @__PURE__ */ new Map(), Zr = "abort auxClick beforeToggle cancel canPlay canPlayThrough click close contextMenu copy cut drag dragEnd dragEnter dragExit dragLeave dragOver dragStart drop durationChange emptied encrypted ended error gotPointerCapture input invalid keyDown keyPress keyUp load loadedData loadedMetadata loadStart lostPointerCapture mouseDown mouseMove mouseOut mouseOver mouseUp paste pause play playing pointerCancel pointerDown pointerMove pointerOut pointerOver pointerUp progress rateChange reset resize seeked seeking stalled submit suspend timeUpdate touchCancel touchEnd touchStart volumeChange scroll toggle touchMove waiting wheel".split(" ");
	Zr.push("scrollEnd");
	function Qr(e, t) {
		Xr.set(e, t), At(t, [e]);
	}
	var $r = typeof reportError == "function" ? reportError : function(e) {
		if (typeof window == "object" && typeof window.ErrorEvent == "function") {
			var t = new window.ErrorEvent("error", {
				bubbles: !0,
				cancelable: !0,
				message: typeof e == "object" && e && typeof e.message == "string" ? String(e.message) : String(e),
				error: e
			});
			if (!window.dispatchEvent(t)) return;
		} else if (typeof process == "object" && typeof process.emit == "function") {
			process.emit("uncaughtException", e);
			return;
		}
		console.error(e);
	}, ei = [], ti = 0, ni = 0;
	function ri() {
		for (var e = ti, t = ni = ti = 0; t < e;) {
			var n = ei[t];
			ei[t++] = null;
			var r = ei[t];
			ei[t++] = null;
			var i = ei[t];
			ei[t++] = null;
			var a = ei[t];
			if (ei[t++] = null, r !== null && i !== null) {
				var o = r.pending;
				o === null ? i.next = i : (i.next = o.next, o.next = i), r.pending = i;
			}
			a !== 0 && si(n, i, a);
		}
	}
	function ii(e, t, n, r) {
		ei[ti++] = e, ei[ti++] = t, ei[ti++] = n, ei[ti++] = r, ni |= r, e.lanes |= r, e = e.alternate, e !== null && (e.lanes |= r);
	}
	function ai(e, t, n, r) {
		return ii(e, t, n, r), ci(e);
	}
	function oi(e, t) {
		return ii(e, null, null, t), ci(e);
	}
	function si(e, t, n) {
		e.lanes |= n;
		var r = e.alternate;
		r !== null && (r.lanes |= n);
		for (var i = !1, a = e.return; a !== null;) a.childLanes |= n, r = a.alternate, r !== null && (r.childLanes |= n), a.tag === 22 && (e = a.stateNode, e === null || e._visibility & 1 || (i = !0)), e = a, a = a.return;
		return e.tag === 3 ? (a = e.stateNode, i && t !== null && (i = 31 - Ge(n), e = a.hiddenUpdates, r = e[i], r === null ? e[i] = [t] : r.push(t), t.lane = n | 536870912), a) : null;
	}
	function ci(e) {
		if (50 < lu) throw lu = 0, uu = null, Error(i(185));
		for (var t = e.return; t !== null;) e = t, t = e.return;
		return e.tag === 3 ? e.stateNode : null;
	}
	var li = {};
	function ui(e, t, n, r) {
		this.tag = e, this.key = n, this.sibling = this.child = this.return = this.stateNode = this.type = this.elementType = null, this.index = 0, this.refCleanup = this.ref = null, this.pendingProps = t, this.dependencies = this.memoizedState = this.updateQueue = this.memoizedProps = null, this.mode = r, this.subtreeFlags = this.flags = 0, this.deletions = null, this.childLanes = this.lanes = 0, this.alternate = null;
	}
	function di(e, t, n, r) {
		return new ui(e, t, n, r);
	}
	function fi(e) {
		return e = e.prototype, !(!e || !e.isReactComponent);
	}
	function pi(e, t) {
		var n = e.alternate;
		return n === null ? (n = di(e.tag, t, e.key, e.mode), n.elementType = e.elementType, n.type = e.type, n.stateNode = e.stateNode, n.alternate = e, e.alternate = n) : (n.pendingProps = t, n.type = e.type, n.flags = 0, n.subtreeFlags = 0, n.deletions = null), n.flags = e.flags & 65011712, n.childLanes = e.childLanes, n.lanes = e.lanes, n.child = e.child, n.memoizedProps = e.memoizedProps, n.memoizedState = e.memoizedState, n.updateQueue = e.updateQueue, t = e.dependencies, n.dependencies = t === null ? null : {
			lanes: t.lanes,
			firstContext: t.firstContext
		}, n.sibling = e.sibling, n.index = e.index, n.ref = e.ref, n.refCleanup = e.refCleanup, n;
	}
	function I(e, t) {
		e.flags &= 65011714;
		var n = e.alternate;
		return n === null ? (e.childLanes = 0, e.lanes = t, e.child = null, e.subtreeFlags = 0, e.memoizedProps = null, e.memoizedState = null, e.updateQueue = null, e.dependencies = null, e.stateNode = null) : (e.childLanes = n.childLanes, e.lanes = n.lanes, e.child = n.child, e.subtreeFlags = 0, e.deletions = null, e.memoizedProps = n.memoizedProps, e.memoizedState = n.memoizedState, e.updateQueue = n.updateQueue, e.type = n.type, t = n.dependencies, e.dependencies = t === null ? null : {
			lanes: t.lanes,
			firstContext: t.firstContext
		}), e;
	}
	function L(e, t, n, r, a, o) {
		var s = 0;
		if (r = e, typeof e == "function") fi(e) && (s = 1);
		else if (typeof e == "string") s = Uf(e, n, me.current) ? 26 : e === "html" || e === "head" || e === "body" ? 27 : 5;
		else a: switch (e) {
			case te: return e = di(31, n, t, a), e.elementType = te, e.lanes = o, e;
			case y: return R(n.children, a, o, t);
			case b:
				s = 8, a |= 24;
				break;
			case x: return e = di(12, n, t, a | 2), e.elementType = x, e.lanes = o, e;
			case T: return e = di(13, n, t, a), e.elementType = T, e.lanes = o, e;
			case E: return e = di(19, n, t, a), e.elementType = E, e.lanes = o, e;
			default:
				if (typeof e == "object" && e) switch (e.$$typeof) {
					case C:
						s = 10;
						break a;
					case S:
						s = 9;
						break a;
					case w:
						s = 11;
						break a;
					case ee:
						s = 14;
						break a;
					case D:
						s = 16, r = null;
						break a;
				}
				s = 29, n = Error(i(130, e === null ? "null" : typeof e, "")), r = null;
		}
		return t = di(s, n, t, a), t.elementType = e, t.type = r, t.lanes = o, t;
	}
	function R(e, t, n, r) {
		return e = di(7, e, r, t), e.lanes = n, e;
	}
	function z(e, t, n) {
		return e = di(6, e, null, t), e.lanes = n, e;
	}
	function B(e) {
		var t = di(18, null, null, 0);
		return t.stateNode = e, t;
	}
	function mi(e, t, n) {
		return t = di(4, e.children === null ? [] : e.children, e.key, t), t.lanes = n, t.stateNode = {
			containerInfo: e.containerInfo,
			pendingChildren: null,
			implementation: e.implementation
		}, t;
	}
	var V = /* @__PURE__ */ new WeakMap();
	function hi(e, t) {
		if (typeof e == "object" && e) {
			var n = V.get(e);
			return n === void 0 ? (t = {
				value: e,
				source: t,
				stack: Oe(t)
			}, V.set(e, t), t) : n;
		}
		return {
			value: e,
			source: t,
			stack: Oe(t)
		};
	}
	var gi = [], H = 0, _i = null, vi = 0, yi = [], U = 0, bi = null, W = 1, xi = "";
	function Si(e, t) {
		gi[H++] = vi, gi[H++] = _i, _i = e, vi = t;
	}
	function Ci(e, t, n) {
		yi[U++] = W, yi[U++] = xi, yi[U++] = bi, bi = e;
		var r = W;
		e = xi;
		var i = 32 - Ge(r) - 1;
		r &= ~(1 << i), n += 1;
		var a = 32 - Ge(t) + i;
		if (30 < a) {
			var o = i - i % 5;
			a = (r & (1 << o) - 1).toString(32), r >>= o, i -= o, W = 1 << 32 - Ge(t) + i | n << i | r, xi = a + e;
		} else W = 1 << a | n << i | r, xi = e;
	}
	function wi(e) {
		e.return !== null && (Si(e, 1), Ci(e, 1, 0));
	}
	function Ti(e) {
		for (; e === _i;) _i = gi[--H], gi[H] = null, vi = gi[--H], gi[H] = null;
		for (; e === bi;) bi = yi[--U], yi[U] = null, xi = yi[--U], yi[U] = null, W = yi[--U], yi[U] = null;
	}
	function Ei(e, t) {
		yi[U++] = W, yi[U++] = xi, yi[U++] = bi, W = t.id, xi = t.overflow, bi = e;
	}
	var Di = null, Oi = null, ki = !1, Ai = null, G = !1, ji = Error(i(519));
	function Mi(e) {
		throw Ri(hi(Error(i(418, 1 < arguments.length && arguments[1] !== void 0 && arguments[1] ? "text" : "HTML", "")), e)), ji;
	}
	function Ni(e) {
		var t = e.stateNode, n = e.type, r = e.memoizedProps;
		switch (t[mt] = e, t[ht] = r, n) {
			case "dialog":
				vd("cancel", t), vd("close", t);
				break;
			case "iframe":
			case "object":
			case "embed":
				vd("load", t);
				break;
			case "video":
			case "audio":
				for (n = 0; n < hd.length; n++) vd(hd[n], t);
				break;
			case "source":
				vd("error", t);
				break;
			case "img":
			case "image":
			case "link":
				vd("error", t), vd("load", t);
				break;
			case "details":
				vd("toggle", t);
				break;
			case "input":
				vd("invalid", t), Kt(t, r.value, r.defaultValue, r.checked, r.defaultChecked, r.type, r.name, !0);
				break;
			case "select":
				vd("invalid", t);
				break;
			case "textarea": vd("invalid", t), Xt(t, r.value, r.defaultValue, r.children);
		}
		n = r.children, typeof n != "string" && typeof n != "number" && typeof n != "bigint" || t.textContent === "" + n || !0 === r.suppressHydrationWarning || jd(t.textContent, n) ? (r.popover != null && (vd("beforetoggle", t), vd("toggle", t)), r.onScroll != null && vd("scroll", t), r.onScrollEnd != null && vd("scrollend", t), r.onClick != null && (t.onclick = on), t = !0) : t = !1, t || Mi(e, !0);
	}
	function Pi(e) {
		for (Di = e.return; Di;) switch (Di.tag) {
			case 5:
			case 31:
			case 13:
				G = !1;
				return;
			case 27:
			case 3:
				G = !0;
				return;
			default: Di = Di.return;
		}
	}
	function Fi(e) {
		if (e !== Di) return !1;
		if (!ki) return Pi(e), ki = !0, !1;
		var t = e.tag, n;
		if ((n = t !== 3 && t !== 27) && ((n = t === 5) && (n = e.type, n = !(n !== "form" && n !== "button") || Ud(e.type, e.memoizedProps)), n = !n), n && Oi && Mi(e), Pi(e), t === 13) {
			if (e = e.memoizedState, e = e === null ? null : e.dehydrated, !e) throw Error(i(317));
			Oi = uf(e);
		} else if (t === 31) {
			if (e = e.memoizedState, e = e === null ? null : e.dehydrated, !e) throw Error(i(317));
			Oi = uf(e);
		} else t === 27 ? (t = Oi, Zd(e.type) ? (e = lf, lf = null, Oi = e) : Oi = t) : Oi = Di ? cf(e.stateNode.nextSibling) : null;
		return !0;
	}
	function Ii() {
		Oi = Di = null, ki = !1;
	}
	function Li() {
		var e = Ai;
		return e !== null && (Yl === null ? Yl = e : Yl.push.apply(Yl, e), Ai = null), e;
	}
	function Ri(e) {
		Ai === null ? Ai = [e] : Ai.push(e);
	}
	var zi = de(null), Bi = null, Vi = null;
	function Hi(e, t, n) {
		pe(zi, t._currentValue), t._currentValue = n;
	}
	function Ui(e) {
		e._currentValue = zi.current, fe(zi);
	}
	function Wi(e, t, n) {
		for (; e !== null;) {
			var r = e.alternate;
			if ((e.childLanes & t) === t ? r !== null && (r.childLanes & t) !== t && (r.childLanes |= t) : (e.childLanes |= t, r !== null && (r.childLanes |= t)), e === n) break;
			e = e.return;
		}
	}
	function Gi(e, t, n, r) {
		var a = e.child;
		for (a !== null && (a.return = e); a !== null;) {
			var o = a.dependencies;
			if (o !== null) {
				var s = a.child;
				o = o.firstContext;
				a: for (; o !== null;) {
					var c = o;
					o = a;
					for (var l = 0; l < t.length; l++) if (c.context === t[l]) {
						o.lanes |= n, c = o.alternate, c !== null && (c.lanes |= n), Wi(o.return, n, e), r || (s = null);
						break a;
					}
					o = c.next;
				}
			} else if (a.tag === 18) {
				if (s = a.return, s === null) throw Error(i(341));
				s.lanes |= n, o = s.alternate, o !== null && (o.lanes |= n), Wi(s, n, e), s = null;
			} else s = a.child;
			if (s !== null) s.return = a;
			else for (s = a; s !== null;) {
				if (s === e) {
					s = null;
					break;
				}
				if (a = s.sibling, a !== null) {
					a.return = s.return, s = a;
					break;
				}
				s = s.return;
			}
			a = s;
		}
	}
	function Ki(e, t, n, r) {
		e = null;
		for (var a = t, o = !1; a !== null;) {
			if (!o) {
				if (a.flags & 524288) o = !0;
				else if (a.flags & 262144) break;
			}
			if (a.tag === 10) {
				var s = a.alternate;
				if (s === null) throw Error(i(387));
				if (s = s.memoizedProps, s !== null) {
					var c = a.type;
					Er(a.pendingProps.value, s.value) || (e === null ? e = [c] : e.push(c));
				}
			} else if (a === _e.current) {
				if (s = a.alternate, s === null) throw Error(i(387));
				s.memoizedState.memoizedState !== a.memoizedState.memoizedState && (e === null ? e = [Qf] : e.push(Qf));
			}
			a = a.return;
		}
		e !== null && Gi(t, e, n, r), t.flags |= 262144;
	}
	function qi(e) {
		for (e = e.firstContext; e !== null;) {
			if (!Er(e.context._currentValue, e.memoizedValue)) return !0;
			e = e.next;
		}
		return !1;
	}
	function Ji(e) {
		Bi = e, Vi = null, e = e.dependencies, e !== null && (e.firstContext = null);
	}
	function Yi(e) {
		return Zi(Bi, e);
	}
	function Xi(e, t) {
		return Bi === null && Ji(e), Zi(e, t);
	}
	function Zi(e, t) {
		var n = t._currentValue;
		if (t = {
			context: t,
			memoizedValue: n,
			next: null
		}, Vi === null) {
			if (e === null) throw Error(i(308));
			Vi = t, e.dependencies = {
				lanes: 0,
				firstContext: t
			}, e.flags |= 524288;
		} else Vi = Vi.next = t;
		return n;
	}
	var Qi = typeof AbortController < "u" ? AbortController : function() {
		var e = [], t = this.signal = {
			aborted: !1,
			addEventListener: function(t, n) {
				e.push(n);
			}
		};
		this.abort = function() {
			t.aborted = !0, e.forEach(function(e) {
				return e();
			});
		};
	}, $i = t.unstable_scheduleCallback, ea = t.unstable_NormalPriority, ta = {
		$$typeof: C,
		Consumer: null,
		Provider: null,
		_currentValue: null,
		_currentValue2: null,
		_threadCount: 0
	};
	function na() {
		return {
			controller: new Qi(),
			data: /* @__PURE__ */ new Map(),
			refCount: 0
		};
	}
	function ra(e) {
		e.refCount--, e.refCount === 0 && $i(ea, function() {
			e.controller.abort();
		});
	}
	var ia = null, aa = 0, oa = 0, K = null;
	function sa(e, t) {
		if (ia === null) {
			var n = ia = [];
			aa = 0, oa = ld(), K = {
				status: "pending",
				value: void 0,
				then: function(e) {
					n.push(e);
				}
			};
		}
		return aa++, t.then(ca, ca), t;
	}
	function ca() {
		if (--aa === 0 && ia !== null) {
			K !== null && (K.status = "fulfilled");
			var e = ia;
			ia = null, oa = 0, K = null;
			for (var t = 0; t < e.length; t++) (0, e[t])();
		}
	}
	function la(e, t) {
		var n = [], r = {
			status: "pending",
			value: null,
			reason: null,
			then: function(e) {
				n.push(e);
			}
		};
		return e.then(function() {
			r.status = "fulfilled", r.value = t;
			for (var e = 0; e < n.length; e++) (0, n[e])(t);
		}, function(e) {
			for (r.status = "rejected", r.reason = e, e = 0; e < n.length; e++) (0, n[e])(void 0);
		}), r;
	}
	var ua = O.S;
	O.S = function(e, t) {
		Ql = Pe(), typeof t == "object" && t && typeof t.then == "function" && sa(e, t), ua !== null && ua(e, t);
	};
	var da = de(null);
	function fa() {
		var e = da.current;
		return e === null ? Nl.pooledCache : e;
	}
	function pa(e, t) {
		t === null ? pe(da, da.current) : pe(da, t.pool);
	}
	function ma() {
		var e = fa();
		return e === null ? null : {
			parent: ta._currentValue,
			pool: e
		};
	}
	var ha = Error(i(460)), ga = Error(i(474)), _a = Error(i(542)), va = { then: function() {} };
	function ya(e) {
		return e = e.status, e === "fulfilled" || e === "rejected";
	}
	function ba(e, t, n) {
		switch (n = e[n], n === void 0 ? e.push(t) : n !== t && (t.then(on, on), t = n), t.status) {
			case "fulfilled": return t.value;
			case "rejected": throw e = t.reason, wa(e), e;
			default:
				if (typeof t.status == "string") t.then(on, on);
				else {
					if (e = Nl, e !== null && 100 < e.shellSuspendCounter) throw Error(i(482));
					e = t, e.status = "pending", e.then(function(e) {
						if (t.status === "pending") {
							var n = t;
							n.status = "fulfilled", n.value = e;
						}
					}, function(e) {
						if (t.status === "pending") {
							var n = t;
							n.status = "rejected", n.reason = e;
						}
					});
				}
				switch (t.status) {
					case "fulfilled": return t.value;
					case "rejected": throw e = t.reason, wa(e), e;
				}
				throw Sa = t, ha;
		}
	}
	function xa(e) {
		try {
			var t = e._init;
			return t(e._payload);
		} catch (e) {
			throw typeof e == "object" && e && typeof e.then == "function" ? (Sa = e, ha) : e;
		}
	}
	var Sa = null;
	function Ca() {
		if (Sa === null) throw Error(i(459));
		var e = Sa;
		return Sa = null, e;
	}
	function wa(e) {
		if (e === ha || e === _a) throw Error(i(483));
	}
	var Ta = null, Ea = 0;
	function q(e) {
		var t = Ea;
		return Ea += 1, Ta === null && (Ta = []), ba(Ta, e, t);
	}
	function Da(e, t) {
		t = t.props.ref, e.ref = t === void 0 ? null : t;
	}
	function Oa(e, t) {
		throw t.$$typeof === g ? Error(i(525)) : (e = Object.prototype.toString.call(t), Error(i(31, e === "[object Object]" ? "object with keys {" + Object.keys(t).join(", ") + "}" : e)));
	}
	function ka(e) {
		function t(t, n) {
			if (e) {
				var r = t.deletions;
				r === null ? (t.deletions = [n], t.flags |= 16) : r.push(n);
			}
		}
		function n(n, r) {
			if (!e) return null;
			for (; r !== null;) t(n, r), r = r.sibling;
			return null;
		}
		function r(e) {
			for (var t = /* @__PURE__ */ new Map(); e !== null;) e.key === null ? t.set(e.index, e) : t.set(e.key, e), e = e.sibling;
			return t;
		}
		function a(e, t) {
			return e = pi(e, t), e.index = 0, e.sibling = null, e;
		}
		function o(t, n, r) {
			return t.index = r, e ? (r = t.alternate, r === null ? (t.flags |= 67108866, n) : (r = r.index, r < n ? (t.flags |= 67108866, n) : r)) : (t.flags |= 1048576, n);
		}
		function s(t) {
			return e && t.alternate === null && (t.flags |= 67108866), t;
		}
		function c(e, t, n, r) {
			return t === null || t.tag !== 6 ? (t = z(n, e.mode, r), t.return = e, t) : (t = a(t, n), t.return = e, t);
		}
		function l(e, t, n, r) {
			var i = n.type;
			return i === y ? d(e, t, n.props.children, r, n.key) : t !== null && (t.elementType === i || typeof i == "object" && i && i.$$typeof === D && xa(i) === t.type) ? (t = a(t, n.props), Da(t, n), t.return = e, t) : (t = L(n.type, n.key, n.props, null, e.mode, r), Da(t, n), t.return = e, t);
		}
		function u(e, t, n, r) {
			return t === null || t.tag !== 4 || t.stateNode.containerInfo !== n.containerInfo || t.stateNode.implementation !== n.implementation ? (t = mi(n, e.mode, r), t.return = e, t) : (t = a(t, n.children || []), t.return = e, t);
		}
		function d(e, t, n, r, i) {
			return t === null || t.tag !== 7 ? (t = R(n, e.mode, r, i), t.return = e, t) : (t = a(t, n), t.return = e, t);
		}
		function f(e, t, n) {
			if (typeof t == "string" && t !== "" || typeof t == "number" || typeof t == "bigint") return t = z("" + t, e.mode, n), t.return = e, t;
			if (typeof t == "object" && t) {
				switch (t.$$typeof) {
					case _: return n = L(t.type, t.key, t.props, null, e.mode, n), Da(n, t), n.return = e, n;
					case v: return t = mi(t, e.mode, n), t.return = e, t;
					case D: return t = xa(t), f(e, t, n);
				}
				if (se(t) || ie(t)) return t = R(t, e.mode, n, null), t.return = e, t;
				if (typeof t.then == "function") return f(e, q(t), n);
				if (t.$$typeof === C) return f(e, Xi(e, t), n);
				Oa(e, t);
			}
			return null;
		}
		function p(e, t, n, r) {
			var i = t === null ? null : t.key;
			if (typeof n == "string" && n !== "" || typeof n == "number" || typeof n == "bigint") return i === null ? c(e, t, "" + n, r) : null;
			if (typeof n == "object" && n) {
				switch (n.$$typeof) {
					case _: return n.key === i ? l(e, t, n, r) : null;
					case v: return n.key === i ? u(e, t, n, r) : null;
					case D: return n = xa(n), p(e, t, n, r);
				}
				if (se(n) || ie(n)) return i === null ? d(e, t, n, r, null) : null;
				if (typeof n.then == "function") return p(e, t, q(n), r);
				if (n.$$typeof === C) return p(e, t, Xi(e, n), r);
				Oa(e, n);
			}
			return null;
		}
		function m(e, t, n, r, i) {
			if (typeof r == "string" && r !== "" || typeof r == "number" || typeof r == "bigint") return e = e.get(n) || null, c(t, e, "" + r, i);
			if (typeof r == "object" && r) {
				switch (r.$$typeof) {
					case _: return e = e.get(r.key === null ? n : r.key) || null, l(t, e, r, i);
					case v: return e = e.get(r.key === null ? n : r.key) || null, u(t, e, r, i);
					case D: return r = xa(r), m(e, t, n, r, i);
				}
				if (se(r) || ie(r)) return e = e.get(n) || null, d(t, e, r, i, null);
				if (typeof r.then == "function") return m(e, t, n, q(r), i);
				if (r.$$typeof === C) return m(e, t, n, Xi(t, r), i);
				Oa(t, r);
			}
			return null;
		}
		function h(i, a, s, c) {
			for (var l = null, u = null, d = a, h = a = 0, g = null; d !== null && h < s.length; h++) {
				d.index > h ? (g = d, d = null) : g = d.sibling;
				var _ = p(i, d, s[h], c);
				if (_ === null) {
					d === null && (d = g);
					break;
				}
				e && d && _.alternate === null && t(i, d), a = o(_, a, h), u === null ? l = _ : u.sibling = _, u = _, d = g;
			}
			if (h === s.length) return n(i, d), ki && Si(i, h), l;
			if (d === null) {
				for (; h < s.length; h++) d = f(i, s[h], c), d !== null && (a = o(d, a, h), u === null ? l = d : u.sibling = d, u = d);
				return ki && Si(i, h), l;
			}
			for (d = r(d); h < s.length; h++) g = m(d, i, h, s[h], c), g !== null && (e && g.alternate !== null && d.delete(g.key === null ? h : g.key), a = o(g, a, h), u === null ? l = g : u.sibling = g, u = g);
			return e && d.forEach(function(e) {
				return t(i, e);
			}), ki && Si(i, h), l;
		}
		function g(a, s, c, l) {
			if (c == null) throw Error(i(151));
			for (var u = null, d = null, h = s, g = s = 0, _ = null, v = c.next(); h !== null && !v.done; g++, v = c.next()) {
				h.index > g ? (_ = h, h = null) : _ = h.sibling;
				var y = p(a, h, v.value, l);
				if (y === null) {
					h === null && (h = _);
					break;
				}
				e && h && y.alternate === null && t(a, h), s = o(y, s, g), d === null ? u = y : d.sibling = y, d = y, h = _;
			}
			if (v.done) return n(a, h), ki && Si(a, g), u;
			if (h === null) {
				for (; !v.done; g++, v = c.next()) v = f(a, v.value, l), v !== null && (s = o(v, s, g), d === null ? u = v : d.sibling = v, d = v);
				return ki && Si(a, g), u;
			}
			for (h = r(h); !v.done; g++, v = c.next()) v = m(h, a, g, v.value, l), v !== null && (e && v.alternate !== null && h.delete(v.key === null ? g : v.key), s = o(v, s, g), d === null ? u = v : d.sibling = v, d = v);
			return e && h.forEach(function(e) {
				return t(a, e);
			}), ki && Si(a, g), u;
		}
		function b(e, r, o, c) {
			if (typeof o == "object" && o && o.type === y && o.key === null && (o = o.props.children), typeof o == "object" && o) {
				switch (o.$$typeof) {
					case _:
						a: {
							for (var l = o.key; r !== null;) {
								if (r.key === l) {
									if (l = o.type, l === y) {
										if (r.tag === 7) {
											n(e, r.sibling), c = a(r, o.props.children), c.return = e, e = c;
											break a;
										}
									} else if (r.elementType === l || typeof l == "object" && l && l.$$typeof === D && xa(l) === r.type) {
										n(e, r.sibling), c = a(r, o.props), Da(c, o), c.return = e, e = c;
										break a;
									}
									n(e, r);
									break;
								} else t(e, r);
								r = r.sibling;
							}
							o.type === y ? (c = R(o.props.children, e.mode, c, o.key), c.return = e, e = c) : (c = L(o.type, o.key, o.props, null, e.mode, c), Da(c, o), c.return = e, e = c);
						}
						return s(e);
					case v:
						a: {
							for (l = o.key; r !== null;) {
								if (r.key === l) if (r.tag === 4 && r.stateNode.containerInfo === o.containerInfo && r.stateNode.implementation === o.implementation) {
									n(e, r.sibling), c = a(r, o.children || []), c.return = e, e = c;
									break a;
								} else {
									n(e, r);
									break;
								}
								else t(e, r);
								r = r.sibling;
							}
							c = mi(o, e.mode, c), c.return = e, e = c;
						}
						return s(e);
					case D: return o = xa(o), b(e, r, o, c);
				}
				if (se(o)) return h(e, r, o, c);
				if (ie(o)) {
					if (l = ie(o), typeof l != "function") throw Error(i(150));
					return o = l.call(o), g(e, r, o, c);
				}
				if (typeof o.then == "function") return b(e, r, q(o), c);
				if (o.$$typeof === C) return b(e, r, Xi(e, o), c);
				Oa(e, o);
			}
			return typeof o == "string" && o !== "" || typeof o == "number" || typeof o == "bigint" ? (o = "" + o, r !== null && r.tag === 6 ? (n(e, r.sibling), c = a(r, o), c.return = e, e = c) : (n(e, r), c = z(o, e.mode, c), c.return = e, e = c), s(e)) : n(e, r);
		}
		return function(e, t, n, r) {
			try {
				Ea = 0;
				var i = b(e, t, n, r);
				return Ta = null, i;
			} catch (t) {
				if (t === ha || t === _a) throw t;
				var a = di(29, t, null, e.mode);
				return a.lanes = r, a.return = e, a;
			}
		};
	}
	var Aa = ka(!0), ja = ka(!1), Ma = !1;
	function Na(e) {
		e.updateQueue = {
			baseState: e.memoizedState,
			firstBaseUpdate: null,
			lastBaseUpdate: null,
			shared: {
				pending: null,
				lanes: 0,
				hiddenCallbacks: null
			},
			callbacks: null
		};
	}
	function Pa(e, t) {
		e = e.updateQueue, t.updateQueue === e && (t.updateQueue = {
			baseState: e.baseState,
			firstBaseUpdate: e.firstBaseUpdate,
			lastBaseUpdate: e.lastBaseUpdate,
			shared: e.shared,
			callbacks: null
		});
	}
	function Fa(e) {
		return {
			lane: e,
			tag: 0,
			payload: null,
			callback: null,
			next: null
		};
	}
	function Ia(e, t, n) {
		var r = e.updateQueue;
		if (r === null) return null;
		if (r = r.shared, Ml & 2) {
			var i = r.pending;
			return i === null ? t.next = t : (t.next = i.next, i.next = t), r.pending = t, t = ci(e), si(e, null, n), t;
		}
		return ii(e, r, t, n), ci(e);
	}
	function La(e, t, n) {
		if (t = t.updateQueue, t !== null && (t = t.shared, n & 4194048)) {
			var r = t.lanes;
			r &= e.pendingLanes, n |= r, t.lanes = n, st(e, n);
		}
	}
	function Ra(e, t) {
		var n = e.updateQueue, r = e.alternate;
		if (r !== null && (r = r.updateQueue, n === r)) {
			var i = null, a = null;
			if (n = n.firstBaseUpdate, n !== null) {
				do {
					var o = {
						lane: n.lane,
						tag: n.tag,
						payload: n.payload,
						callback: null,
						next: null
					};
					a === null ? i = a = o : a = a.next = o, n = n.next;
				} while (n !== null);
				a === null ? i = a = t : a = a.next = t;
			} else i = a = t;
			n = {
				baseState: r.baseState,
				firstBaseUpdate: i,
				lastBaseUpdate: a,
				shared: r.shared,
				callbacks: r.callbacks
			}, e.updateQueue = n;
			return;
		}
		e = n.lastBaseUpdate, e === null ? n.firstBaseUpdate = t : e.next = t, n.lastBaseUpdate = t;
	}
	var za = !1;
	function Ba() {
		if (za) {
			var e = K;
			if (e !== null) throw e;
		}
	}
	function Va(e, t, n, r) {
		za = !1;
		var i = e.updateQueue;
		Ma = !1;
		var a = i.firstBaseUpdate, o = i.lastBaseUpdate, s = i.shared.pending;
		if (s !== null) {
			i.shared.pending = null;
			var c = s, l = c.next;
			c.next = null, o === null ? a = l : o.next = l, o = c;
			var u = e.alternate;
			u !== null && (u = u.updateQueue, s = u.lastBaseUpdate, s !== o && (s === null ? u.firstBaseUpdate = l : s.next = l, u.lastBaseUpdate = c));
		}
		if (a !== null) {
			var d = i.baseState;
			o = 0, u = l = c = null, s = a;
			do {
				var f = s.lane & -536870913, p = f !== s.lane;
				if (p ? (Fl & f) === f : (r & f) === f) {
					f !== 0 && f === oa && (za = !0), u !== null && (u = u.next = {
						lane: 0,
						tag: s.tag,
						payload: s.payload,
						callback: null,
						next: null
					});
					a: {
						var h = e, g = s;
						f = t;
						var _ = n;
						switch (g.tag) {
							case 1:
								if (h = g.payload, typeof h == "function") {
									d = h.call(_, d, f);
									break a;
								}
								d = h;
								break a;
							case 3: h.flags = h.flags & -65537 | 128;
							case 0:
								if (h = g.payload, f = typeof h == "function" ? h.call(_, d, f) : h, f == null) break a;
								d = m({}, d, f);
								break a;
							case 2: Ma = !0;
						}
					}
					f = s.callback, f !== null && (e.flags |= 64, p && (e.flags |= 8192), p = i.callbacks, p === null ? i.callbacks = [f] : p.push(f));
				} else p = {
					lane: f,
					tag: s.tag,
					payload: s.payload,
					callback: s.callback,
					next: null
				}, u === null ? (l = u = p, c = d) : u = u.next = p, o |= f;
				if (s = s.next, s === null) {
					if (s = i.shared.pending, s === null) break;
					p = s, s = p.next, p.next = null, i.lastBaseUpdate = p, i.shared.pending = null;
				}
			} while (1);
			u === null && (c = d), i.baseState = c, i.firstBaseUpdate = l, i.lastBaseUpdate = u, a === null && (i.shared.lanes = 0), Ul |= o, e.lanes = o, e.memoizedState = d;
		}
	}
	function Ha(e, t) {
		if (typeof e != "function") throw Error(i(191, e));
		e.call(t);
	}
	function Ua(e, t) {
		var n = e.callbacks;
		if (n !== null) for (e.callbacks = null, e = 0; e < n.length; e++) Ha(n[e], t);
	}
	var J = de(null), Wa = de(0);
	function Ga(e, t) {
		e = Vl, pe(Wa, e), pe(J, t), Vl = e | t.baseLanes;
	}
	function Ka() {
		pe(Wa, Vl), pe(J, J.current);
	}
	function qa() {
		Vl = Wa.current, fe(J), fe(Wa);
	}
	var Ja = de(null), Ya = null;
	function Xa(e) {
		var t = e.alternate;
		pe(Y, Y.current & 1), pe(Ja, e), Ya === null && (t === null || J.current !== null || t.memoizedState !== null) && (Ya = e);
	}
	function Za(e) {
		pe(Y, Y.current), pe(Ja, e), Ya === null && (Ya = e);
	}
	function Qa(e) {
		e.tag === 22 ? (pe(Y, Y.current), pe(Ja, e), Ya === null && (Ya = e)) : $a(e);
	}
	function $a() {
		pe(Y, Y.current), pe(Ja, Ja.current);
	}
	function eo(e) {
		fe(Ja), Ya === e && (Ya = null), fe(Y);
	}
	var Y = de(0);
	function to(e) {
		for (var t = e; t !== null;) {
			if (t.tag === 13) {
				var n = t.memoizedState;
				if (n !== null && (n = n.dehydrated, n === null || af(n) || of(n))) return t;
			} else if (t.tag === 19 && (t.memoizedProps.revealOrder === "forwards" || t.memoizedProps.revealOrder === "backwards" || t.memoizedProps.revealOrder === "unstable_legacy-backwards" || t.memoizedProps.revealOrder === "together")) {
				if (t.flags & 128) return t;
			} else if (t.child !== null) {
				t.child.return = t, t = t.child;
				continue;
			}
			if (t === e) break;
			for (; t.sibling === null;) {
				if (t.return === null || t.return === e) return null;
				t = t.return;
			}
			t.sibling.return = t.return, t = t.sibling;
		}
		return null;
	}
	var no = 0, X = null, ro = null, io = null, ao = !1, oo = !1, so = !1, co = 0, lo = 0, uo = null, fo = 0;
	function po() {
		throw Error(i(321));
	}
	function mo(e, t) {
		if (t === null) return !1;
		for (var n = 0; n < t.length && n < e.length; n++) if (!Er(e[n], t[n])) return !1;
		return !0;
	}
	function ho(e, t, n, r, i, a) {
		return no = a, X = t, t.memoizedState = null, t.updateQueue = null, t.lanes = 0, O.H = e === null || e.memoizedState === null ? As : js, so = !1, a = n(r, i), so = !1, oo && (a = _o(t, n, r, i)), go(e), a;
	}
	function go(e) {
		O.H = ks;
		var t = ro !== null && ro.next !== null;
		if (no = 0, io = ro = X = null, ao = !1, lo = 0, uo = null, t) throw Error(i(300));
		e === null || Js || (e = e.dependencies, e !== null && qi(e) && (Js = !0));
	}
	function _o(e, t, n, r) {
		X = e;
		var a = 0;
		do {
			if (oo && (uo = null), lo = 0, oo = !1, 25 <= a) throw Error(i(301));
			if (a += 1, io = ro = null, e.updateQueue != null) {
				var o = e.updateQueue;
				o.lastEffect = null, o.events = null, o.stores = null, o.memoCache != null && (o.memoCache.index = 0);
			}
			O.H = Ms, o = t(n, r);
		} while (oo);
		return o;
	}
	function vo() {
		var e = O.H, t = e.useState()[0];
		return t = typeof t.then == "function" ? wo(t) : t, e = e.useState()[0], (ro === null ? null : ro.memoizedState) !== e && (X.flags |= 1024), t;
	}
	function Z() {
		var e = co !== 0;
		return co = 0, e;
	}
	function yo(e, t, n) {
		t.updateQueue = e.updateQueue, t.flags &= -2053, e.lanes &= ~n;
	}
	function bo(e) {
		if (ao) {
			for (e = e.memoizedState; e !== null;) {
				var t = e.queue;
				t !== null && (t.pending = null), e = e.next;
			}
			ao = !1;
		}
		no = 0, io = ro = X = null, oo = !1, lo = co = 0, uo = null;
	}
	function xo() {
		var e = {
			memoizedState: null,
			baseState: null,
			baseQueue: null,
			queue: null,
			next: null
		};
		return io === null ? X.memoizedState = io = e : io = io.next = e, io;
	}
	function So() {
		if (ro === null) {
			var e = X.alternate;
			e = e === null ? null : e.memoizedState;
		} else e = ro.next;
		var t = io === null ? X.memoizedState : io.next;
		if (t !== null) io = t, ro = e;
		else {
			if (e === null) throw X.alternate === null ? Error(i(467)) : Error(i(310));
			ro = e, e = {
				memoizedState: ro.memoizedState,
				baseState: ro.baseState,
				baseQueue: ro.baseQueue,
				queue: ro.queue,
				next: null
			}, io === null ? X.memoizedState = io = e : io = io.next = e;
		}
		return io;
	}
	function Co() {
		return {
			lastEffect: null,
			events: null,
			stores: null,
			memoCache: null
		};
	}
	function wo(e) {
		var t = lo;
		return lo += 1, uo === null && (uo = []), e = ba(uo, e, t), t = X, (io === null ? t.memoizedState : io.next) === null && (t = t.alternate, O.H = t === null || t.memoizedState === null ? As : js), e;
	}
	function To(e) {
		if (typeof e == "object" && e) {
			if (typeof e.then == "function") return wo(e);
			if (e.$$typeof === C) return Yi(e);
		}
		throw Error(i(438, String(e)));
	}
	function Eo(e) {
		var t = null, n = X.updateQueue;
		if (n !== null && (t = n.memoCache), t == null) {
			var r = X.alternate;
			r !== null && (r = r.updateQueue, r !== null && (r = r.memoCache, r != null && (t = {
				data: r.data.map(function(e) {
					return e.slice();
				}),
				index: 0
			})));
		}
		if (t ??= {
			data: [],
			index: 0
		}, n === null && (n = Co(), X.updateQueue = n), n.memoCache = t, n = t.data[t.index], n === void 0) for (n = t.data[t.index] = Array(e), r = 0; r < e; r++) n[r] = ne;
		return t.index++, n;
	}
	function Do(e, t) {
		return typeof t == "function" ? t(e) : t;
	}
	function Oo(e) {
		return ko(So(), ro, e);
	}
	function ko(e, t, n) {
		var r = e.queue;
		if (r === null) throw Error(i(311));
		r.lastRenderedReducer = n;
		var a = e.baseQueue, o = r.pending;
		if (o !== null) {
			if (a !== null) {
				var s = a.next;
				a.next = o.next, o.next = s;
			}
			t.baseQueue = a = o, r.pending = null;
		}
		if (o = e.baseState, a === null) e.memoizedState = o;
		else {
			t = a.next;
			var c = s = null, l = null, u = t, d = !1;
			do {
				var f = u.lane & -536870913;
				if (f === u.lane ? (no & f) === f : (Fl & f) === f) {
					var p = u.revertLane;
					if (p === 0) l !== null && (l = l.next = {
						lane: 0,
						revertLane: 0,
						gesture: null,
						action: u.action,
						hasEagerState: u.hasEagerState,
						eagerState: u.eagerState,
						next: null
					}), f === oa && (d = !0);
					else if ((no & p) === p) {
						u = u.next, p === oa && (d = !0);
						continue;
					} else f = {
						lane: 0,
						revertLane: u.revertLane,
						gesture: null,
						action: u.action,
						hasEagerState: u.hasEagerState,
						eagerState: u.eagerState,
						next: null
					}, l === null ? (c = l = f, s = o) : l = l.next = f, X.lanes |= p, Ul |= p;
					f = u.action, so && n(o, f), o = u.hasEagerState ? u.eagerState : n(o, f);
				} else p = {
					lane: f,
					revertLane: u.revertLane,
					gesture: u.gesture,
					action: u.action,
					hasEagerState: u.hasEagerState,
					eagerState: u.eagerState,
					next: null
				}, l === null ? (c = l = p, s = o) : l = l.next = p, X.lanes |= f, Ul |= f;
				u = u.next;
			} while (u !== null && u !== t);
			if (l === null ? s = o : l.next = c, !Er(o, e.memoizedState) && (Js = !0, d && (n = K, n !== null))) throw n;
			e.memoizedState = o, e.baseState = s, e.baseQueue = l, r.lastRenderedState = o;
		}
		return a === null && (r.lanes = 0), [e.memoizedState, r.dispatch];
	}
	function Ao(e) {
		var t = So(), n = t.queue;
		if (n === null) throw Error(i(311));
		n.lastRenderedReducer = e;
		var r = n.dispatch, a = n.pending, o = t.memoizedState;
		if (a !== null) {
			n.pending = null;
			var s = a = a.next;
			do
				o = e(o, s.action), s = s.next;
			while (s !== a);
			Er(o, t.memoizedState) || (Js = !0), t.memoizedState = o, t.baseQueue === null && (t.baseState = o), n.lastRenderedState = o;
		}
		return [o, r];
	}
	function jo(e, t, n) {
		var r = X, a = So(), o = ki;
		if (o) {
			if (n === void 0) throw Error(i(407));
			n = n();
		} else n = t();
		var s = !Er((ro || a).memoizedState, n);
		if (s && (a.memoizedState = n, Js = !0), a = a.queue, ns(Po.bind(null, r, a, e), [e]), a.getSnapshot !== t || s || io !== null && io.memoizedState.tag & 1) {
			if (r.flags |= 2048, Zo(9, { destroy: void 0 }, No.bind(null, r, a, n, t), null), Nl === null) throw Error(i(349));
			o || no & 127 || Mo(r, t, n);
		}
		return n;
	}
	function Mo(e, t, n) {
		e.flags |= 16384, e = {
			getSnapshot: t,
			value: n
		}, t = X.updateQueue, t === null ? (t = Co(), X.updateQueue = t, t.stores = [e]) : (n = t.stores, n === null ? t.stores = [e] : n.push(e));
	}
	function No(e, t, n, r) {
		t.value = n, t.getSnapshot = r, Fo(t) && Io(e);
	}
	function Po(e, t, n) {
		return n(function() {
			Fo(t) && Io(e);
		});
	}
	function Fo(e) {
		var t = e.getSnapshot;
		e = e.value;
		try {
			var n = t();
			return !Er(e, n);
		} catch {
			return !0;
		}
	}
	function Io(e) {
		var t = oi(e, 2);
		t !== null && pu(t, e, 2);
	}
	function Lo(e) {
		var t = xo();
		if (typeof e == "function") {
			var n = e;
			if (e = n(), so) {
				We(!0);
				try {
					n();
				} finally {
					We(!1);
				}
			}
		}
		return t.memoizedState = t.baseState = e, t.queue = {
			pending: null,
			lanes: 0,
			dispatch: null,
			lastRenderedReducer: Do,
			lastRenderedState: e
		}, t;
	}
	function Ro(e, t, n, r) {
		return e.baseState = n, ko(e, ro, typeof r == "function" ? r : Do);
	}
	function zo(e, t, n, r, a) {
		if (Es(e)) throw Error(i(485));
		if (e = t.action, e !== null) {
			var o = {
				payload: a,
				action: e,
				next: null,
				isTransition: !0,
				status: "pending",
				value: null,
				reason: null,
				listeners: [],
				then: function(e) {
					o.listeners.push(e);
				}
			};
			O.T === null ? o.isTransition = !1 : n(!0), r(o), n = t.pending, n === null ? (o.next = t.pending = o, Bo(t, o)) : (o.next = n.next, t.pending = n.next = o);
		}
	}
	function Bo(e, t) {
		var n = t.action, r = t.payload, i = e.state;
		if (t.isTransition) {
			var a = O.T, o = {};
			O.T = o;
			try {
				var s = n(i, r), c = O.S;
				c !== null && c(o, s), Vo(e, t, s);
			} catch (n) {
				Uo(e, t, n);
			} finally {
				a !== null && o.types !== null && (a.types = o.types), O.T = a;
			}
		} else try {
			a = n(i, r), Vo(e, t, a);
		} catch (n) {
			Uo(e, t, n);
		}
	}
	function Vo(e, t, n) {
		typeof n == "object" && n && typeof n.then == "function" ? n.then(function(n) {
			Ho(e, t, n);
		}, function(n) {
			return Uo(e, t, n);
		}) : Ho(e, t, n);
	}
	function Ho(e, t, n) {
		t.status = "fulfilled", t.value = n, Wo(t), e.state = n, t = e.pending, t !== null && (n = t.next, n === t ? e.pending = null : (n = n.next, t.next = n, Bo(e, n)));
	}
	function Uo(e, t, n) {
		var r = e.pending;
		if (e.pending = null, r !== null) {
			r = r.next;
			do
				t.status = "rejected", t.reason = n, Wo(t), t = t.next;
			while (t !== r);
		}
		e.action = null;
	}
	function Wo(e) {
		e = e.listeners;
		for (var t = 0; t < e.length; t++) (0, e[t])();
	}
	function Go(e, t) {
		return t;
	}
	function Ko(e, t) {
		if (ki) {
			var n = Nl.formState;
			if (n !== null) {
				a: {
					var r = X;
					if (ki) {
						if (Oi) {
							b: {
								for (var i = Oi, a = G; i.nodeType !== 8;) {
									if (!a) {
										i = null;
										break b;
									}
									if (i = cf(i.nextSibling), i === null) {
										i = null;
										break b;
									}
								}
								a = i.data, i = a === "F!" || a === "F" ? i : null;
							}
							if (i) {
								Oi = cf(i.nextSibling), r = i.data === "F!";
								break a;
							}
						}
						Mi(r);
					}
					r = !1;
				}
				r && (t = n[0]);
			}
		}
		return n = xo(), n.memoizedState = n.baseState = t, r = {
			pending: null,
			lanes: 0,
			dispatch: null,
			lastRenderedReducer: Go,
			lastRenderedState: t
		}, n.queue = r, n = ws.bind(null, X, r), r.dispatch = n, r = Lo(!1), a = Q.bind(null, X, !1, r.queue), r = xo(), i = {
			state: t,
			dispatch: null,
			action: e,
			pending: null
		}, r.queue = i, n = zo.bind(null, X, i, a, n), i.dispatch = n, r.memoizedState = e, [
			t,
			n,
			!1
		];
	}
	function qo(e) {
		return Jo(So(), ro, e);
	}
	function Jo(e, t, n) {
		if (t = ko(e, t, Go)[0], e = Oo(Do)[0], typeof t == "object" && t && typeof t.then == "function") try {
			var r = wo(t);
		} catch (e) {
			throw e === ha ? _a : e;
		}
		else r = t;
		t = So();
		var i = t.queue, a = i.dispatch;
		return n !== t.memoizedState && (X.flags |= 2048, Zo(9, { destroy: void 0 }, Yo.bind(null, i, n), null)), [
			r,
			a,
			e
		];
	}
	function Yo(e, t) {
		e.action = t;
	}
	function Xo(e) {
		var t = So(), n = ro;
		if (n !== null) return Jo(t, n, e);
		So(), t = t.memoizedState, n = So();
		var r = n.queue.dispatch;
		return n.memoizedState = e, [
			t,
			r,
			!1
		];
	}
	function Zo(e, t, n, r) {
		return e = {
			tag: e,
			create: n,
			deps: r,
			inst: t,
			next: null
		}, t = X.updateQueue, t === null && (t = Co(), X.updateQueue = t), n = t.lastEffect, n === null ? t.lastEffect = e.next = e : (r = n.next, n.next = e, e.next = r, t.lastEffect = e), e;
	}
	function Qo() {
		return So().memoizedState;
	}
	function $o(e, t, n, r) {
		var i = xo();
		X.flags |= e, i.memoizedState = Zo(1 | t, { destroy: void 0 }, n, r === void 0 ? null : r);
	}
	function es(e, t, n, r) {
		var i = So();
		r = r === void 0 ? null : r;
		var a = i.memoizedState.inst;
		ro !== null && r !== null && mo(r, ro.memoizedState.deps) ? i.memoizedState = Zo(t, a, n, r) : (X.flags |= e, i.memoizedState = Zo(1 | t, a, n, r));
	}
	function ts(e, t) {
		$o(8390656, 8, e, t);
	}
	function ns(e, t) {
		es(2048, 8, e, t);
	}
	function rs(e) {
		X.flags |= 4;
		var t = X.updateQueue;
		if (t === null) t = Co(), X.updateQueue = t, t.events = [e];
		else {
			var n = t.events;
			n === null ? t.events = [e] : n.push(e);
		}
	}
	function is(e) {
		var t = So().memoizedState;
		return rs({
			ref: t,
			nextImpl: e
		}), function() {
			if (Ml & 2) throw Error(i(440));
			return t.impl.apply(void 0, arguments);
		};
	}
	function as(e, t) {
		return es(4, 2, e, t);
	}
	function os(e, t) {
		return es(4, 4, e, t);
	}
	function ss(e, t) {
		if (typeof t == "function") {
			e = e();
			var n = t(e);
			return function() {
				typeof n == "function" ? n() : t(null);
			};
		}
		if (t != null) return e = e(), t.current = e, function() {
			t.current = null;
		};
	}
	function cs(e, t, n) {
		n = n == null ? null : n.concat([e]), es(4, 4, ss.bind(null, t, e), n);
	}
	function ls() {}
	function us(e, t) {
		var n = So();
		t = t === void 0 ? null : t;
		var r = n.memoizedState;
		return t !== null && mo(t, r[1]) ? r[0] : (n.memoizedState = [e, t], e);
	}
	function ds(e, t) {
		var n = So();
		t = t === void 0 ? null : t;
		var r = n.memoizedState;
		if (t !== null && mo(t, r[1])) return r[0];
		if (r = e(), so) {
			We(!0);
			try {
				e();
			} finally {
				We(!1);
			}
		}
		return n.memoizedState = [r, t], r;
	}
	function fs(e, t, n) {
		return n === void 0 || no & 1073741824 && !(Fl & 261930) ? e.memoizedState = t : (e.memoizedState = n, e = fu(), X.lanes |= e, Ul |= e, n);
	}
	function ps(e, t, n, r) {
		return Er(n, t) ? n : J.current === null ? !(no & 42) || no & 1073741824 && !(Fl & 261930) ? (Js = !0, e.memoizedState = n) : (e = fu(), X.lanes |= e, Ul |= e, t) : (e = fs(e, n, r), Er(e, t) || (Js = !0), e);
	}
	function ms(e, t, n, r, i) {
		var a = k.p;
		k.p = a !== 0 && 8 > a ? a : 8;
		var o = O.T, s = {};
		O.T = s, Q(e, !1, t, n);
		try {
			var c = i(), l = O.S;
			l !== null && l(s, c), typeof c == "object" && c && typeof c.then == "function" ? Ts(e, t, la(c, r), du(e)) : Ts(e, t, r, du(e));
		} catch (n) {
			Ts(e, t, {
				then: function() {},
				status: "rejected",
				reason: n
			}, du());
		} finally {
			k.p = a, o !== null && s.types !== null && (o.types = s.types), O.T = o;
		}
	}
	function hs() {}
	function gs(e, t, n, r) {
		if (e.tag !== 5) throw Error(i(476));
		var a = _s(e).queue;
		ms(e, a, t, ce, n === null ? hs : function() {
			return vs(e), n(r);
		});
	}
	function _s(e) {
		var t = e.memoizedState;
		if (t !== null) return t;
		t = {
			memoizedState: ce,
			baseState: ce,
			baseQueue: null,
			queue: {
				pending: null,
				lanes: 0,
				dispatch: null,
				lastRenderedReducer: Do,
				lastRenderedState: ce
			},
			next: null
		};
		var n = {};
		return t.next = {
			memoizedState: n,
			baseState: n,
			baseQueue: null,
			queue: {
				pending: null,
				lanes: 0,
				dispatch: null,
				lastRenderedReducer: Do,
				lastRenderedState: n
			},
			next: null
		}, e.memoizedState = t, e = e.alternate, e !== null && (e.memoizedState = t), t;
	}
	function vs(e) {
		var t = _s(e);
		t.next === null && (t = e.alternate.memoizedState), Ts(e, t.next.queue, {}, du());
	}
	function ys() {
		return Yi(Qf);
	}
	function bs() {
		return So().memoizedState;
	}
	function xs() {
		return So().memoizedState;
	}
	function Ss(e) {
		for (var t = e.return; t !== null;) {
			switch (t.tag) {
				case 24:
				case 3:
					var n = du();
					e = Fa(n);
					var r = Ia(t, e, n);
					r !== null && (pu(r, t, n), La(r, t, n)), t = { cache: na() }, e.payload = t;
					return;
			}
			t = t.return;
		}
	}
	function Cs(e, t, n) {
		var r = du();
		n = {
			lane: r,
			revertLane: 0,
			gesture: null,
			action: n,
			hasEagerState: !1,
			eagerState: null,
			next: null
		}, Es(e) ? Ds(t, n) : (n = ai(e, t, n, r), n !== null && (pu(n, e, r), Os(n, t, r)));
	}
	function ws(e, t, n) {
		Ts(e, t, n, du());
	}
	function Ts(e, t, n, r) {
		var i = {
			lane: r,
			revertLane: 0,
			gesture: null,
			action: n,
			hasEagerState: !1,
			eagerState: null,
			next: null
		};
		if (Es(e)) Ds(t, i);
		else {
			var a = e.alternate;
			if (e.lanes === 0 && (a === null || a.lanes === 0) && (a = t.lastRenderedReducer, a !== null)) try {
				var o = t.lastRenderedState, s = a(o, n);
				if (i.hasEagerState = !0, i.eagerState = s, Er(s, o)) return ii(e, t, i, 0), Nl === null && ri(), !1;
			} catch {}
			if (n = ai(e, t, i, r), n !== null) return pu(n, e, r), Os(n, t, r), !0;
		}
		return !1;
	}
	function Q(e, t, n, r) {
		if (r = {
			lane: 2,
			revertLane: ld(),
			gesture: null,
			action: r,
			hasEagerState: !1,
			eagerState: null,
			next: null
		}, Es(e)) {
			if (t) throw Error(i(479));
		} else t = ai(e, n, r, 2), t !== null && pu(t, e, 2);
	}
	function Es(e) {
		var t = e.alternate;
		return e === X || t !== null && t === X;
	}
	function Ds(e, t) {
		oo = ao = !0;
		var n = e.pending;
		n === null ? t.next = t : (t.next = n.next, n.next = t), e.pending = t;
	}
	function Os(e, t, n) {
		if (n & 4194048) {
			var r = t.lanes;
			r &= e.pendingLanes, n |= r, t.lanes = n, st(e, n);
		}
	}
	var ks = {
		readContext: Yi,
		use: To,
		useCallback: po,
		useContext: po,
		useEffect: po,
		useImperativeHandle: po,
		useLayoutEffect: po,
		useInsertionEffect: po,
		useMemo: po,
		useReducer: po,
		useRef: po,
		useState: po,
		useDebugValue: po,
		useDeferredValue: po,
		useTransition: po,
		useSyncExternalStore: po,
		useId: po,
		useHostTransitionStatus: po,
		useFormState: po,
		useActionState: po,
		useOptimistic: po,
		useMemoCache: po,
		useCacheRefresh: po
	};
	ks.useEffectEvent = po;
	var As = {
		readContext: Yi,
		use: To,
		useCallback: function(e, t) {
			return xo().memoizedState = [e, t === void 0 ? null : t], e;
		},
		useContext: Yi,
		useEffect: ts,
		useImperativeHandle: function(e, t, n) {
			n = n == null ? null : n.concat([e]), $o(4194308, 4, ss.bind(null, t, e), n);
		},
		useLayoutEffect: function(e, t) {
			return $o(4194308, 4, e, t);
		},
		useInsertionEffect: function(e, t) {
			$o(4, 2, e, t);
		},
		useMemo: function(e, t) {
			var n = xo();
			t = t === void 0 ? null : t;
			var r = e();
			if (so) {
				We(!0);
				try {
					e();
				} finally {
					We(!1);
				}
			}
			return n.memoizedState = [r, t], r;
		},
		useReducer: function(e, t, n) {
			var r = xo();
			if (n !== void 0) {
				var i = n(t);
				if (so) {
					We(!0);
					try {
						n(t);
					} finally {
						We(!1);
					}
				}
			} else i = t;
			return r.memoizedState = r.baseState = i, e = {
				pending: null,
				lanes: 0,
				dispatch: null,
				lastRenderedReducer: e,
				lastRenderedState: i
			}, r.queue = e, e = e.dispatch = Cs.bind(null, X, e), [r.memoizedState, e];
		},
		useRef: function(e) {
			var t = xo();
			return e = { current: e }, t.memoizedState = e;
		},
		useState: function(e) {
			e = Lo(e);
			var t = e.queue, n = ws.bind(null, X, t);
			return t.dispatch = n, [e.memoizedState, n];
		},
		useDebugValue: ls,
		useDeferredValue: function(e, t) {
			return fs(xo(), e, t);
		},
		useTransition: function() {
			var e = Lo(!1);
			return e = ms.bind(null, X, e.queue, !0, !1), xo().memoizedState = e, [!1, e];
		},
		useSyncExternalStore: function(e, t, n) {
			var r = X, a = xo();
			if (ki) {
				if (n === void 0) throw Error(i(407));
				n = n();
			} else {
				if (n = t(), Nl === null) throw Error(i(349));
				Fl & 127 || Mo(r, t, n);
			}
			a.memoizedState = n;
			var o = {
				value: n,
				getSnapshot: t
			};
			return a.queue = o, ts(Po.bind(null, r, o, e), [e]), r.flags |= 2048, Zo(9, { destroy: void 0 }, No.bind(null, r, o, n, t), null), n;
		},
		useId: function() {
			var e = xo(), t = Nl.identifierPrefix;
			if (ki) {
				var n = xi, r = W;
				n = (r & ~(1 << 32 - Ge(r) - 1)).toString(32) + n, t = "_" + t + "R_" + n, n = co++, 0 < n && (t += "H" + n.toString(32)), t += "_";
			} else n = fo++, t = "_" + t + "r_" + n.toString(32) + "_";
			return e.memoizedState = t;
		},
		useHostTransitionStatus: ys,
		useFormState: Ko,
		useActionState: Ko,
		useOptimistic: function(e) {
			var t = xo();
			t.memoizedState = t.baseState = e;
			var n = {
				pending: null,
				lanes: 0,
				dispatch: null,
				lastRenderedReducer: null,
				lastRenderedState: null
			};
			return t.queue = n, t = Q.bind(null, X, !0, n), n.dispatch = t, [e, t];
		},
		useMemoCache: Eo,
		useCacheRefresh: function() {
			return xo().memoizedState = Ss.bind(null, X);
		},
		useEffectEvent: function(e) {
			var t = xo(), n = { impl: e };
			return t.memoizedState = n, function() {
				if (Ml & 2) throw Error(i(440));
				return n.impl.apply(void 0, arguments);
			};
		}
	}, js = {
		readContext: Yi,
		use: To,
		useCallback: us,
		useContext: Yi,
		useEffect: ns,
		useImperativeHandle: cs,
		useInsertionEffect: as,
		useLayoutEffect: os,
		useMemo: ds,
		useReducer: Oo,
		useRef: Qo,
		useState: function() {
			return Oo(Do);
		},
		useDebugValue: ls,
		useDeferredValue: function(e, t) {
			return ps(So(), ro.memoizedState, e, t);
		},
		useTransition: function() {
			var e = Oo(Do)[0], t = So().memoizedState;
			return [typeof e == "boolean" ? e : wo(e), t];
		},
		useSyncExternalStore: jo,
		useId: bs,
		useHostTransitionStatus: ys,
		useFormState: qo,
		useActionState: qo,
		useOptimistic: function(e, t) {
			return Ro(So(), ro, e, t);
		},
		useMemoCache: Eo,
		useCacheRefresh: xs
	};
	js.useEffectEvent = is;
	var Ms = {
		readContext: Yi,
		use: To,
		useCallback: us,
		useContext: Yi,
		useEffect: ns,
		useImperativeHandle: cs,
		useInsertionEffect: as,
		useLayoutEffect: os,
		useMemo: ds,
		useReducer: Ao,
		useRef: Qo,
		useState: function() {
			return Ao(Do);
		},
		useDebugValue: ls,
		useDeferredValue: function(e, t) {
			var n = So();
			return ro === null ? fs(n, e, t) : ps(n, ro.memoizedState, e, t);
		},
		useTransition: function() {
			var e = Ao(Do)[0], t = So().memoizedState;
			return [typeof e == "boolean" ? e : wo(e), t];
		},
		useSyncExternalStore: jo,
		useId: bs,
		useHostTransitionStatus: ys,
		useFormState: Xo,
		useActionState: Xo,
		useOptimistic: function(e, t) {
			var n = So();
			return ro === null ? (n.baseState = e, [e, n.queue.dispatch]) : Ro(n, ro, e, t);
		},
		useMemoCache: Eo,
		useCacheRefresh: xs
	};
	Ms.useEffectEvent = is;
	function Ns(e, t, n, r) {
		t = e.memoizedState, n = n(r, t), n = n == null ? t : m({}, t, n), e.memoizedState = n, e.lanes === 0 && (e.updateQueue.baseState = n);
	}
	var Ps = {
		enqueueSetState: function(e, t, n) {
			e = e._reactInternals;
			var r = du(), i = Fa(r);
			i.payload = t, n != null && (i.callback = n), t = Ia(e, i, r), t !== null && (pu(t, e, r), La(t, e, r));
		},
		enqueueReplaceState: function(e, t, n) {
			e = e._reactInternals;
			var r = du(), i = Fa(r);
			i.tag = 1, i.payload = t, n != null && (i.callback = n), t = Ia(e, i, r), t !== null && (pu(t, e, r), La(t, e, r));
		},
		enqueueForceUpdate: function(e, t) {
			e = e._reactInternals;
			var n = du(), r = Fa(n);
			r.tag = 2, t != null && (r.callback = t), t = Ia(e, r, n), t !== null && (pu(t, e, n), La(t, e, n));
		}
	};
	function Fs(e, t, n, r, i, a, o) {
		return e = e.stateNode, typeof e.shouldComponentUpdate == "function" ? e.shouldComponentUpdate(r, a, o) : t.prototype && t.prototype.isPureReactComponent ? !Dr(n, r) || !Dr(i, a) : !0;
	}
	function Is(e, t, n, r) {
		e = t.state, typeof t.componentWillReceiveProps == "function" && t.componentWillReceiveProps(n, r), typeof t.UNSAFE_componentWillReceiveProps == "function" && t.UNSAFE_componentWillReceiveProps(n, r), t.state !== e && Ps.enqueueReplaceState(t, t.state, null);
	}
	function Ls(e, t) {
		var n = t;
		if ("ref" in t) for (var r in n = {}, t) r !== "ref" && (n[r] = t[r]);
		if (e = e.defaultProps) for (var i in n === t && (n = m({}, n)), e) n[i] === void 0 && (n[i] = e[i]);
		return n;
	}
	function Rs(e) {
		$r(e);
	}
	function zs(e) {
		console.error(e);
	}
	function Bs(e) {
		$r(e);
	}
	function Vs(e, t) {
		try {
			var n = e.onUncaughtError;
			n(t.value, { componentStack: t.stack });
		} catch (e) {
			setTimeout(function() {
				throw e;
			});
		}
	}
	function Hs(e, t, n) {
		try {
			var r = e.onCaughtError;
			r(n.value, {
				componentStack: n.stack,
				errorBoundary: t.tag === 1 ? t.stateNode : null
			});
		} catch (e) {
			setTimeout(function() {
				throw e;
			});
		}
	}
	function Us(e, t, n) {
		return n = Fa(n), n.tag = 3, n.payload = { element: null }, n.callback = function() {
			Vs(e, t);
		}, n;
	}
	function Ws(e) {
		return e = Fa(e), e.tag = 3, e;
	}
	function Gs(e, t, n, r) {
		var i = n.type.getDerivedStateFromError;
		if (typeof i == "function") {
			var a = r.value;
			e.payload = function() {
				return i(a);
			}, e.callback = function() {
				Hs(t, n, r);
			};
		}
		var o = n.stateNode;
		o !== null && typeof o.componentDidCatch == "function" && (e.callback = function() {
			Hs(t, n, r), typeof i != "function" && (tu === null ? tu = new Set([this]) : tu.add(this));
			var e = r.stack;
			this.componentDidCatch(r.value, { componentStack: e === null ? "" : e });
		});
	}
	function Ks(e, t, n, r, a) {
		if (n.flags |= 32768, typeof r == "object" && r && typeof r.then == "function") {
			if (t = n.alternate, t !== null && Ki(t, n, a, !0), n = Ja.current, n !== null) {
				switch (n.tag) {
					case 31:
					case 13: return Ya === null ? wu() : n.alternate === null && Hl === 0 && (Hl = 3), n.flags &= -257, n.flags |= 65536, n.lanes = a, r === va ? n.flags |= 16384 : (t = n.updateQueue, t === null ? n.updateQueue = new Set([r]) : t.add(r), Uu(e, r, a)), !1;
					case 22: return n.flags |= 65536, r === va ? n.flags |= 16384 : (t = n.updateQueue, t === null ? (t = {
						transitions: null,
						markerInstances: null,
						retryQueue: new Set([r])
					}, n.updateQueue = t) : (n = t.retryQueue, n === null ? t.retryQueue = new Set([r]) : n.add(r)), Uu(e, r, a)), !1;
				}
				throw Error(i(435, n.tag));
			}
			return Uu(e, r, a), wu(), !1;
		}
		if (ki) return t = Ja.current, t === null ? (r !== ji && (t = Error(i(423), { cause: r }), Ri(hi(t, n))), e = e.current.alternate, e.flags |= 65536, a &= -a, e.lanes |= a, r = hi(r, n), a = Us(e.stateNode, r, a), Ra(e, a), Hl !== 4 && (Hl = 2)) : (!(t.flags & 65536) && (t.flags |= 256), t.flags |= 65536, t.lanes = a, r !== ji && (e = Error(i(422), { cause: r }), Ri(hi(e, n)))), !1;
		var o = Error(i(520), { cause: r });
		if (o = hi(o, n), Jl === null ? Jl = [o] : Jl.push(o), Hl !== 4 && (Hl = 2), t === null) return !0;
		r = hi(r, n), n = t;
		do {
			switch (n.tag) {
				case 3: return n.flags |= 65536, e = a & -a, n.lanes |= e, e = Us(n.stateNode, r, e), Ra(n, e), !1;
				case 1: if (t = n.type, o = n.stateNode, !(n.flags & 128) && (typeof t.getDerivedStateFromError == "function" || o !== null && typeof o.componentDidCatch == "function" && (tu === null || !tu.has(o)))) return n.flags |= 65536, a &= -a, n.lanes |= a, a = Ws(a), Gs(a, e, n, r), Ra(n, a), !1;
			}
			n = n.return;
		} while (n !== null);
		return !1;
	}
	var qs = Error(i(461)), Js = !1;
	function Ys(e, t, n, r) {
		t.child = e === null ? ja(t, null, n, r) : Aa(t, e.child, n, r);
	}
	function Xs(e, t, n, r, i) {
		n = n.render;
		var a = t.ref;
		if ("ref" in r) {
			var o = {};
			for (var s in r) s !== "ref" && (o[s] = r[s]);
		} else o = r;
		return Ji(t), r = ho(e, t, n, o, a, i), s = Z(), e !== null && !Js ? (yo(e, t, i), bc(e, t, i)) : (ki && s && wi(t), t.flags |= 1, Ys(e, t, r, i), t.child);
	}
	function Zs(e, t, n, r, i) {
		if (e === null) {
			var a = n.type;
			return typeof a == "function" && !fi(a) && a.defaultProps === void 0 && n.compare === null ? (t.tag = 15, t.type = a, Qs(e, t, a, r, i)) : (e = L(n.type, null, r, t, t.mode, i), e.ref = t.ref, e.return = t, t.child = e);
		}
		if (a = e.child, !xc(e, i)) {
			var o = a.memoizedProps;
			if (n = n.compare, n = n === null ? Dr : n, n(o, r) && e.ref === t.ref) return bc(e, t, i);
		}
		return t.flags |= 1, e = pi(a, r), e.ref = t.ref, e.return = t, t.child = e;
	}
	function Qs(e, t, n, r, i) {
		if (e !== null) {
			var a = e.memoizedProps;
			if (Dr(a, r) && e.ref === t.ref) if (Js = !1, t.pendingProps = r = a, xc(e, i)) e.flags & 131072 && (Js = !0);
			else return t.lanes = e.lanes, bc(e, t, i);
		}
		return oc(e, t, n, r, i);
	}
	function $s(e, t, n, r) {
		var i = r.children, a = e === null ? null : e.memoizedState;
		if (e === null && t.stateNode === null && (t.stateNode = {
			_visibility: 1,
			_pendingMarkers: null,
			_retryCache: null,
			_transitions: null
		}), r.mode === "hidden") {
			if (t.flags & 128) {
				if (a = a === null ? n : a.baseLanes | n, e !== null) {
					for (r = t.child = e.child, i = 0; r !== null;) i = i | r.lanes | r.childLanes, r = r.sibling;
					r = i & ~a;
				} else r = 0, t.child = null;
				return tc(e, t, a, n, r);
			}
			if (n & 536870912) t.memoizedState = {
				baseLanes: 0,
				cachePool: null
			}, e !== null && pa(t, a === null ? null : a.cachePool), a === null ? Ka() : Ga(t, a), Qa(t);
			else return r = t.lanes = 536870912, tc(e, t, a === null ? n : a.baseLanes | n, n, r);
		} else a === null ? (e !== null && pa(t, null), Ka(), $a(t)) : (pa(t, a.cachePool), Ga(t, a), $a(t), t.memoizedState = null);
		return Ys(e, t, i, n), t.child;
	}
	function ec(e, t) {
		return e !== null && e.tag === 22 || t.stateNode !== null || (t.stateNode = {
			_visibility: 1,
			_pendingMarkers: null,
			_retryCache: null,
			_transitions: null
		}), t.sibling;
	}
	function tc(e, t, n, r, i) {
		var a = fa();
		return a = a === null ? null : {
			parent: ta._currentValue,
			pool: a
		}, t.memoizedState = {
			baseLanes: n,
			cachePool: a
		}, e !== null && pa(t, null), Ka(), Qa(t), e !== null && Ki(e, t, r, !0), t.childLanes = i, null;
	}
	function nc(e, t) {
		return t = hc({
			mode: t.mode,
			children: t.children
		}, e.mode), t.ref = e.ref, e.child = t, t.return = e, t;
	}
	function rc(e, t, n) {
		return Aa(t, e.child, null, n), e = nc(t, t.pendingProps), e.flags |= 2, eo(t), t.memoizedState = null, e;
	}
	function ic(e, t, n) {
		var r = t.pendingProps, a = (t.flags & 128) != 0;
		if (t.flags &= -129, e === null) {
			if (ki) {
				if (r.mode === "hidden") return e = nc(t, r), t.lanes = 536870912, ec(null, e);
				if (Za(t), (e = Oi) ? (e = rf(e, G), e = e !== null && e.data === "&" ? e : null, e !== null && (t.memoizedState = {
					dehydrated: e,
					treeContext: bi === null ? null : {
						id: W,
						overflow: xi
					},
					retryLane: 536870912,
					hydrationErrors: null
				}, n = B(e), n.return = t, t.child = n, Di = t, Oi = null)) : e = null, e === null) throw Mi(t);
				return t.lanes = 536870912, null;
			}
			return nc(t, r);
		}
		var o = e.memoizedState;
		if (o !== null) {
			var s = o.dehydrated;
			if (Za(t), a) if (t.flags & 256) t.flags &= -257, t = rc(e, t, n);
			else if (t.memoizedState !== null) t.child = e.child, t.flags |= 128, t = null;
			else throw Error(i(558));
			else if (Js || Ki(e, t, n, !1), a = (n & e.childLanes) !== 0, Js || a) {
				if (r = Nl, r !== null && (s = ct(r, n), s !== 0 && s !== o.retryLane)) throw o.retryLane = s, oi(e, s), pu(r, e, s), qs;
				wu(), t = rc(e, t, n);
			} else e = o.treeContext, Oi = cf(s.nextSibling), Di = t, ki = !0, Ai = null, G = !1, e !== null && Ei(t, e), t = nc(t, r), t.flags |= 4096;
			return t;
		}
		return e = pi(e.child, {
			mode: r.mode,
			children: r.children
		}), e.ref = t.ref, t.child = e, e.return = t, e;
	}
	function ac(e, t) {
		var n = t.ref;
		if (n === null) e !== null && e.ref !== null && (t.flags |= 4194816);
		else {
			if (typeof n != "function" && typeof n != "object") throw Error(i(284));
			(e === null || e.ref !== n) && (t.flags |= 4194816);
		}
	}
	function oc(e, t, n, r, i) {
		return Ji(t), n = ho(e, t, n, r, void 0, i), r = Z(), e !== null && !Js ? (yo(e, t, i), bc(e, t, i)) : (ki && r && wi(t), t.flags |= 1, Ys(e, t, n, i), t.child);
	}
	function sc(e, t, n, r, i, a) {
		return Ji(t), t.updateQueue = null, n = _o(t, r, n, i), go(e), r = Z(), e !== null && !Js ? (yo(e, t, a), bc(e, t, a)) : (ki && r && wi(t), t.flags |= 1, Ys(e, t, n, a), t.child);
	}
	function cc(e, t, n, r, i) {
		if (Ji(t), t.stateNode === null) {
			var a = li, o = n.contextType;
			typeof o == "object" && o && (a = Yi(o)), a = new n(r, a), t.memoizedState = a.state !== null && a.state !== void 0 ? a.state : null, a.updater = Ps, t.stateNode = a, a._reactInternals = t, a = t.stateNode, a.props = r, a.state = t.memoizedState, a.refs = {}, Na(t), o = n.contextType, a.context = typeof o == "object" && o ? Yi(o) : li, a.state = t.memoizedState, o = n.getDerivedStateFromProps, typeof o == "function" && (Ns(t, n, o, r), a.state = t.memoizedState), typeof n.getDerivedStateFromProps == "function" || typeof a.getSnapshotBeforeUpdate == "function" || typeof a.UNSAFE_componentWillMount != "function" && typeof a.componentWillMount != "function" || (o = a.state, typeof a.componentWillMount == "function" && a.componentWillMount(), typeof a.UNSAFE_componentWillMount == "function" && a.UNSAFE_componentWillMount(), o !== a.state && Ps.enqueueReplaceState(a, a.state, null), Va(t, r, a, i), Ba(), a.state = t.memoizedState), typeof a.componentDidMount == "function" && (t.flags |= 4194308), r = !0;
		} else if (e === null) {
			a = t.stateNode;
			var s = t.memoizedProps, c = Ls(n, s);
			a.props = c;
			var l = a.context, u = n.contextType;
			o = li, typeof u == "object" && u && (o = Yi(u));
			var d = n.getDerivedStateFromProps;
			u = typeof d == "function" || typeof a.getSnapshotBeforeUpdate == "function", s = t.pendingProps !== s, u || typeof a.UNSAFE_componentWillReceiveProps != "function" && typeof a.componentWillReceiveProps != "function" || (s || l !== o) && Is(t, a, r, o), Ma = !1;
			var f = t.memoizedState;
			a.state = f, Va(t, r, a, i), Ba(), l = t.memoizedState, s || f !== l || Ma ? (typeof d == "function" && (Ns(t, n, d, r), l = t.memoizedState), (c = Ma || Fs(t, n, c, r, f, l, o)) ? (u || typeof a.UNSAFE_componentWillMount != "function" && typeof a.componentWillMount != "function" || (typeof a.componentWillMount == "function" && a.componentWillMount(), typeof a.UNSAFE_componentWillMount == "function" && a.UNSAFE_componentWillMount()), typeof a.componentDidMount == "function" && (t.flags |= 4194308)) : (typeof a.componentDidMount == "function" && (t.flags |= 4194308), t.memoizedProps = r, t.memoizedState = l), a.props = r, a.state = l, a.context = o, r = c) : (typeof a.componentDidMount == "function" && (t.flags |= 4194308), r = !1);
		} else {
			a = t.stateNode, Pa(e, t), o = t.memoizedProps, u = Ls(n, o), a.props = u, d = t.pendingProps, f = a.context, l = n.contextType, c = li, typeof l == "object" && l && (c = Yi(l)), s = n.getDerivedStateFromProps, (l = typeof s == "function" || typeof a.getSnapshotBeforeUpdate == "function") || typeof a.UNSAFE_componentWillReceiveProps != "function" && typeof a.componentWillReceiveProps != "function" || (o !== d || f !== c) && Is(t, a, r, c), Ma = !1, f = t.memoizedState, a.state = f, Va(t, r, a, i), Ba();
			var p = t.memoizedState;
			o !== d || f !== p || Ma || e !== null && e.dependencies !== null && qi(e.dependencies) ? (typeof s == "function" && (Ns(t, n, s, r), p = t.memoizedState), (u = Ma || Fs(t, n, u, r, f, p, c) || e !== null && e.dependencies !== null && qi(e.dependencies)) ? (l || typeof a.UNSAFE_componentWillUpdate != "function" && typeof a.componentWillUpdate != "function" || (typeof a.componentWillUpdate == "function" && a.componentWillUpdate(r, p, c), typeof a.UNSAFE_componentWillUpdate == "function" && a.UNSAFE_componentWillUpdate(r, p, c)), typeof a.componentDidUpdate == "function" && (t.flags |= 4), typeof a.getSnapshotBeforeUpdate == "function" && (t.flags |= 1024)) : (typeof a.componentDidUpdate != "function" || o === e.memoizedProps && f === e.memoizedState || (t.flags |= 4), typeof a.getSnapshotBeforeUpdate != "function" || o === e.memoizedProps && f === e.memoizedState || (t.flags |= 1024), t.memoizedProps = r, t.memoizedState = p), a.props = r, a.state = p, a.context = c, r = u) : (typeof a.componentDidUpdate != "function" || o === e.memoizedProps && f === e.memoizedState || (t.flags |= 4), typeof a.getSnapshotBeforeUpdate != "function" || o === e.memoizedProps && f === e.memoizedState || (t.flags |= 1024), r = !1);
		}
		return a = r, ac(e, t), r = (t.flags & 128) != 0, a || r ? (a = t.stateNode, n = r && typeof n.getDerivedStateFromError != "function" ? null : a.render(), t.flags |= 1, e !== null && r ? (t.child = Aa(t, e.child, null, i), t.child = Aa(t, null, n, i)) : Ys(e, t, n, i), t.memoizedState = a.state, e = t.child) : e = bc(e, t, i), e;
	}
	function lc(e, t, n, r) {
		return Ii(), t.flags |= 256, Ys(e, t, n, r), t.child;
	}
	var uc = {
		dehydrated: null,
		treeContext: null,
		retryLane: 0,
		hydrationErrors: null
	};
	function dc(e) {
		return {
			baseLanes: e,
			cachePool: ma()
		};
	}
	function fc(e, t, n) {
		return e = e === null ? 0 : e.childLanes & ~n, t && (e |= Kl), e;
	}
	function pc(e, t, n) {
		var r = t.pendingProps, a = !1, o = (t.flags & 128) != 0, s;
		if ((s = o) || (s = e !== null && e.memoizedState === null ? !1 : (Y.current & 2) != 0), s && (a = !0, t.flags &= -129), s = (t.flags & 32) != 0, t.flags &= -33, e === null) {
			if (ki) {
				if (a ? Xa(t) : $a(t), (e = Oi) ? (e = rf(e, G), e = e !== null && e.data !== "&" ? e : null, e !== null && (t.memoizedState = {
					dehydrated: e,
					treeContext: bi === null ? null : {
						id: W,
						overflow: xi
					},
					retryLane: 536870912,
					hydrationErrors: null
				}, n = B(e), n.return = t, t.child = n, Di = t, Oi = null)) : e = null, e === null) throw Mi(t);
				return of(e) ? t.lanes = 32 : t.lanes = 536870912, null;
			}
			var c = r.children;
			return r = r.fallback, a ? ($a(t), a = t.mode, c = hc({
				mode: "hidden",
				children: c
			}, a), r = R(r, a, n, null), c.return = t, r.return = t, c.sibling = r, t.child = c, r = t.child, r.memoizedState = dc(n), r.childLanes = fc(e, s, n), t.memoizedState = uc, ec(null, r)) : (Xa(t), mc(t, c));
		}
		var l = e.memoizedState;
		if (l !== null && (c = l.dehydrated, c !== null)) {
			if (o) t.flags & 256 ? (Xa(t), t.flags &= -257, t = gc(e, t, n)) : t.memoizedState === null ? ($a(t), c = r.fallback, a = t.mode, r = hc({
				mode: "visible",
				children: r.children
			}, a), c = R(c, a, n, null), c.flags |= 2, r.return = t, c.return = t, r.sibling = c, t.child = r, Aa(t, e.child, null, n), r = t.child, r.memoizedState = dc(n), r.childLanes = fc(e, s, n), t.memoizedState = uc, t = ec(null, r)) : ($a(t), t.child = e.child, t.flags |= 128, t = null);
			else if (Xa(t), of(c)) {
				if (s = c.nextSibling && c.nextSibling.dataset, s) var u = s.dgst;
				s = u, r = Error(i(419)), r.stack = "", r.digest = s, Ri({
					value: r,
					source: null,
					stack: null
				}), t = gc(e, t, n);
			} else if (Js || Ki(e, t, n, !1), s = (n & e.childLanes) !== 0, Js || s) {
				if (s = Nl, s !== null && (r = ct(s, n), r !== 0 && r !== l.retryLane)) throw l.retryLane = r, oi(e, r), pu(s, e, r), qs;
				af(c) || wu(), t = gc(e, t, n);
			} else af(c) ? (t.flags |= 192, t.child = e.child, t = null) : (e = l.treeContext, Oi = cf(c.nextSibling), Di = t, ki = !0, Ai = null, G = !1, e !== null && Ei(t, e), t = mc(t, r.children), t.flags |= 4096);
			return t;
		}
		return a ? ($a(t), c = r.fallback, a = t.mode, l = e.child, u = l.sibling, r = pi(l, {
			mode: "hidden",
			children: r.children
		}), r.subtreeFlags = l.subtreeFlags & 65011712, u === null ? (c = R(c, a, n, null), c.flags |= 2) : c = pi(u, c), c.return = t, r.return = t, r.sibling = c, t.child = r, ec(null, r), r = t.child, c = e.child.memoizedState, c === null ? c = dc(n) : (a = c.cachePool, a === null ? a = ma() : (l = ta._currentValue, a = a.parent === l ? a : {
			parent: l,
			pool: l
		}), c = {
			baseLanes: c.baseLanes | n,
			cachePool: a
		}), r.memoizedState = c, r.childLanes = fc(e, s, n), t.memoizedState = uc, ec(e.child, r)) : (Xa(t), n = e.child, e = n.sibling, n = pi(n, {
			mode: "visible",
			children: r.children
		}), n.return = t, n.sibling = null, e !== null && (s = t.deletions, s === null ? (t.deletions = [e], t.flags |= 16) : s.push(e)), t.child = n, t.memoizedState = null, n);
	}
	function mc(e, t) {
		return t = hc({
			mode: "visible",
			children: t
		}, e.mode), t.return = e, e.child = t;
	}
	function hc(e, t) {
		return e = di(22, e, null, t), e.lanes = 0, e;
	}
	function gc(e, t, n) {
		return Aa(t, e.child, null, n), e = mc(t, t.pendingProps.children), e.flags |= 2, t.memoizedState = null, e;
	}
	function _c(e, t, n) {
		e.lanes |= t;
		var r = e.alternate;
		r !== null && (r.lanes |= t), Wi(e.return, t, n);
	}
	function vc(e, t, n, r, i, a) {
		var o = e.memoizedState;
		o === null ? e.memoizedState = {
			isBackwards: t,
			rendering: null,
			renderingStartTime: 0,
			last: r,
			tail: n,
			tailMode: i,
			treeForkCount: a
		} : (o.isBackwards = t, o.rendering = null, o.renderingStartTime = 0, o.last = r, o.tail = n, o.tailMode = i, o.treeForkCount = a);
	}
	function yc(e, t, n) {
		var r = t.pendingProps, i = r.revealOrder, a = r.tail;
		r = r.children;
		var o = Y.current, s = (o & 2) != 0;
		if (s ? (o = o & 1 | 2, t.flags |= 128) : o &= 1, pe(Y, o), Ys(e, t, r, n), r = ki ? vi : 0, !s && e !== null && e.flags & 128) a: for (e = t.child; e !== null;) {
			if (e.tag === 13) e.memoizedState !== null && _c(e, n, t);
			else if (e.tag === 19) _c(e, n, t);
			else if (e.child !== null) {
				e.child.return = e, e = e.child;
				continue;
			}
			if (e === t) break a;
			for (; e.sibling === null;) {
				if (e.return === null || e.return === t) break a;
				e = e.return;
			}
			e.sibling.return = e.return, e = e.sibling;
		}
		switch (i) {
			case "forwards":
				for (n = t.child, i = null; n !== null;) e = n.alternate, e !== null && to(e) === null && (i = n), n = n.sibling;
				n = i, n === null ? (i = t.child, t.child = null) : (i = n.sibling, n.sibling = null), vc(t, !1, i, n, a, r);
				break;
			case "backwards":
			case "unstable_legacy-backwards":
				for (n = null, i = t.child, t.child = null; i !== null;) {
					if (e = i.alternate, e !== null && to(e) === null) {
						t.child = i;
						break;
					}
					e = i.sibling, i.sibling = n, n = i, i = e;
				}
				vc(t, !0, n, null, a, r);
				break;
			case "together":
				vc(t, !1, null, null, void 0, r);
				break;
			default: t.memoizedState = null;
		}
		return t.child;
	}
	function bc(e, t, n) {
		if (e !== null && (t.dependencies = e.dependencies), Ul |= t.lanes, (n & t.childLanes) === 0) if (e !== null) {
			if (Ki(e, t, n, !1), (n & t.childLanes) === 0) return null;
		} else return null;
		if (e !== null && t.child !== e.child) throw Error(i(153));
		if (t.child !== null) {
			for (e = t.child, n = pi(e, e.pendingProps), t.child = n, n.return = t; e.sibling !== null;) e = e.sibling, n = n.sibling = pi(e, e.pendingProps), n.return = t;
			n.sibling = null;
		}
		return t.child;
	}
	function xc(e, t) {
		return (e.lanes & t) === 0 ? (e = e.dependencies, !!(e !== null && qi(e))) : !0;
	}
	function Sc(e, t, n) {
		switch (t.tag) {
			case 3:
				ve(t, t.stateNode.containerInfo), Hi(t, ta, e.memoizedState.cache), Ii();
				break;
			case 27:
			case 5:
				be(t);
				break;
			case 4:
				ve(t, t.stateNode.containerInfo);
				break;
			case 10:
				Hi(t, t.type, t.memoizedProps.value);
				break;
			case 31:
				if (t.memoizedState !== null) return t.flags |= 128, Za(t), null;
				break;
			case 13:
				var r = t.memoizedState;
				if (r !== null) return r.dehydrated === null ? (n & t.child.childLanes) === 0 ? (Xa(t), e = bc(e, t, n), e === null ? null : e.sibling) : pc(e, t, n) : (Xa(t), t.flags |= 128, null);
				Xa(t);
				break;
			case 19:
				var i = (e.flags & 128) != 0;
				if (r = (n & t.childLanes) !== 0, r ||= (Ki(e, t, n, !1), (n & t.childLanes) !== 0), i) {
					if (r) return yc(e, t, n);
					t.flags |= 128;
				}
				if (i = t.memoizedState, i !== null && (i.rendering = null, i.tail = null, i.lastEffect = null), pe(Y, Y.current), r) break;
				return null;
			case 22: return t.lanes = 0, $s(e, t, n, t.pendingProps);
			case 24: Hi(t, ta, e.memoizedState.cache);
		}
		return bc(e, t, n);
	}
	function Cc(e, t, n) {
		if (e !== null) if (e.memoizedProps !== t.pendingProps) Js = !0;
		else {
			if (!xc(e, n) && !(t.flags & 128)) return Js = !1, Sc(e, t, n);
			Js = !!(e.flags & 131072);
		}
		else Js = !1, ki && t.flags & 1048576 && Ci(t, vi, t.index);
		switch (t.lanes = 0, t.tag) {
			case 16:
				a: {
					var r = t.pendingProps;
					if (e = xa(t.elementType), t.type = e, typeof e == "function") fi(e) ? (r = Ls(e, r), t.tag = 1, t = cc(null, t, e, r, n)) : (t.tag = 0, t = oc(null, t, e, r, n));
					else {
						if (e != null) {
							var a = e.$$typeof;
							if (a === w) {
								t.tag = 11, t = Xs(null, t, e, r, n);
								break a;
							} else if (a === ee) {
								t.tag = 14, t = Zs(null, t, e, r, n);
								break a;
							}
						}
						throw t = oe(e) || e, Error(i(306, t, ""));
					}
				}
				return t;
			case 0: return oc(e, t, t.type, t.pendingProps, n);
			case 1: return r = t.type, a = Ls(r, t.pendingProps), cc(e, t, r, a, n);
			case 3:
				a: {
					if (ve(t, t.stateNode.containerInfo), e === null) throw Error(i(387));
					r = t.pendingProps;
					var o = t.memoizedState;
					a = o.element, Pa(e, t), Va(t, r, null, n);
					var s = t.memoizedState;
					if (r = s.cache, Hi(t, ta, r), r !== o.cache && Gi(t, [ta], n, !0), Ba(), r = s.element, o.isDehydrated) if (o = {
						element: r,
						isDehydrated: !1,
						cache: s.cache
					}, t.updateQueue.baseState = o, t.memoizedState = o, t.flags & 256) {
						t = lc(e, t, r, n);
						break a;
					} else if (r !== a) {
						a = hi(Error(i(424)), t), Ri(a), t = lc(e, t, r, n);
						break a;
					} else {
						switch (e = t.stateNode.containerInfo, e.nodeType) {
							case 9:
								e = e.body;
								break;
							default: e = e.nodeName === "HTML" ? e.ownerDocument.body : e;
						}
						for (Oi = cf(e.firstChild), Di = t, ki = !0, Ai = null, G = !0, n = ja(t, null, r, n), t.child = n; n;) n.flags = n.flags & -3 | 4096, n = n.sibling;
					}
					else {
						if (Ii(), r === a) {
							t = bc(e, t, n);
							break a;
						}
						Ys(e, t, r, n);
					}
					t = t.child;
				}
				return t;
			case 26: return ac(e, t), e === null ? (n = kf(t.type, null, t.pendingProps, null)) ? t.memoizedState = n : ki || (n = t.type, e = t.pendingProps, r = Bd(ge.current).createElement(n), r[mt] = t, r[ht] = e, Pd(r, n, e), Dt(r), t.stateNode = r) : t.memoizedState = kf(t.type, e.memoizedProps, t.pendingProps, e.memoizedState), null;
			case 27: return be(t), e === null && ki && (r = t.stateNode = ff(t.type, t.pendingProps, ge.current), Di = t, G = !0, a = Oi, Zd(t.type) ? (lf = a, Oi = cf(r.firstChild)) : Oi = a), Ys(e, t, t.pendingProps.children, n), ac(e, t), e === null && (t.flags |= 4194304), t.child;
			case 5: return e === null && ki && ((a = r = Oi) && (r = tf(r, t.type, t.pendingProps, G), r === null ? a = !1 : (t.stateNode = r, Di = t, Oi = cf(r.firstChild), G = !1, a = !0)), a || Mi(t)), be(t), a = t.type, o = t.pendingProps, s = e === null ? null : e.memoizedProps, r = o.children, Ud(a, o) ? r = null : s !== null && Ud(a, s) && (t.flags |= 32), t.memoizedState !== null && (a = ho(e, t, vo, null, null, n), Qf._currentValue = a), ac(e, t), Ys(e, t, r, n), t.child;
			case 6: return e === null && ki && ((e = n = Oi) && (n = nf(n, t.pendingProps, G), n === null ? e = !1 : (t.stateNode = n, Di = t, Oi = null, e = !0)), e || Mi(t)), null;
			case 13: return pc(e, t, n);
			case 4: return ve(t, t.stateNode.containerInfo), r = t.pendingProps, e === null ? t.child = Aa(t, null, r, n) : Ys(e, t, r, n), t.child;
			case 11: return Xs(e, t, t.type, t.pendingProps, n);
			case 7: return Ys(e, t, t.pendingProps, n), t.child;
			case 8: return Ys(e, t, t.pendingProps.children, n), t.child;
			case 12: return Ys(e, t, t.pendingProps.children, n), t.child;
			case 10: return r = t.pendingProps, Hi(t, t.type, r.value), Ys(e, t, r.children, n), t.child;
			case 9: return a = t.type._context, r = t.pendingProps.children, Ji(t), a = Yi(a), r = r(a), t.flags |= 1, Ys(e, t, r, n), t.child;
			case 14: return Zs(e, t, t.type, t.pendingProps, n);
			case 15: return Qs(e, t, t.type, t.pendingProps, n);
			case 19: return yc(e, t, n);
			case 31: return ic(e, t, n);
			case 22: return $s(e, t, n, t.pendingProps);
			case 24: return Ji(t), r = Yi(ta), e === null ? (a = fa(), a === null && (a = Nl, o = na(), a.pooledCache = o, o.refCount++, o !== null && (a.pooledCacheLanes |= n), a = o), t.memoizedState = {
				parent: r,
				cache: a
			}, Na(t), Hi(t, ta, a)) : ((e.lanes & n) !== 0 && (Pa(e, t), Va(t, null, null, n), Ba()), a = e.memoizedState, o = t.memoizedState, a.parent === r ? (r = o.cache, Hi(t, ta, r), r !== a.cache && Gi(t, [ta], n, !0)) : (a = {
				parent: r,
				cache: r
			}, t.memoizedState = a, t.lanes === 0 && (t.memoizedState = t.updateQueue.baseState = a), Hi(t, ta, r))), Ys(e, t, t.pendingProps.children, n), t.child;
			case 29: throw t.pendingProps;
		}
		throw Error(i(156, t.tag));
	}
	function wc(e) {
		e.flags |= 4;
	}
	function Tc(e, t, n, r, i) {
		if ((t = (e.mode & 32) != 0) && (t = !1), t) {
			if (e.flags |= 16777216, (i & 335544128) === i) if (e.stateNode.complete) e.flags |= 8192;
			else if ($()) e.flags |= 8192;
			else throw Sa = va, ga;
		} else e.flags &= -16777217;
	}
	function Ec(e, t) {
		if (t.type !== "stylesheet" || t.state.loading & 4) e.flags &= -16777217;
		else if (e.flags |= 16777216, !Wf(t)) if ($()) e.flags |= 8192;
		else throw Sa = va, ga;
	}
	function Dc(e, t) {
		t !== null && (e.flags |= 4), e.flags & 16384 && (t = e.tag === 22 ? 536870912 : nt(), e.lanes |= t, ql |= t);
	}
	function Oc(e, t) {
		if (!ki) switch (e.tailMode) {
			case "hidden":
				t = e.tail;
				for (var n = null; t !== null;) t.alternate !== null && (n = t), t = t.sibling;
				n === null ? e.tail = null : n.sibling = null;
				break;
			case "collapsed":
				n = e.tail;
				for (var r = null; n !== null;) n.alternate !== null && (r = n), n = n.sibling;
				r === null ? t || e.tail === null ? e.tail = null : e.tail.sibling = null : r.sibling = null;
		}
	}
	function kc(e) {
		var t = e.alternate !== null && e.alternate.child === e.child, n = 0, r = 0;
		if (t) for (var i = e.child; i !== null;) n |= i.lanes | i.childLanes, r |= i.subtreeFlags & 65011712, r |= i.flags & 65011712, i.return = e, i = i.sibling;
		else for (i = e.child; i !== null;) n |= i.lanes | i.childLanes, r |= i.subtreeFlags, r |= i.flags, i.return = e, i = i.sibling;
		return e.subtreeFlags |= r, e.childLanes = n, t;
	}
	function Ac(e, t, n) {
		var r = t.pendingProps;
		switch (Ti(t), t.tag) {
			case 16:
			case 15:
			case 0:
			case 11:
			case 7:
			case 8:
			case 12:
			case 9:
			case 14: return kc(t), null;
			case 1: return kc(t), null;
			case 3: return n = t.stateNode, r = null, e !== null && (r = e.memoizedState.cache), t.memoizedState.cache !== r && (t.flags |= 2048), Ui(ta), ye(), n.pendingContext && (n.context = n.pendingContext, n.pendingContext = null), (e === null || e.child === null) && (Fi(t) ? wc(t) : e === null || e.memoizedState.isDehydrated && !(t.flags & 256) || (t.flags |= 1024, Li())), kc(t), null;
			case 26:
				var a = t.type, o = t.memoizedState;
				return e === null ? (wc(t), o === null ? (kc(t), Tc(t, a, null, r, n)) : (kc(t), Ec(t, o))) : o ? o === e.memoizedState ? (kc(t), t.flags &= -16777217) : (wc(t), kc(t), Ec(t, o)) : (e = e.memoizedProps, e !== r && wc(t), kc(t), Tc(t, a, e, r, n)), null;
			case 27:
				if (xe(t), n = ge.current, a = t.type, e !== null && t.stateNode != null) e.memoizedProps !== r && wc(t);
				else {
					if (!r) {
						if (t.stateNode === null) throw Error(i(166));
						return kc(t), null;
					}
					e = me.current, Fi(t) ? Ni(t, e) : (e = ff(a, r, n), t.stateNode = e, wc(t));
				}
				return kc(t), null;
			case 5:
				if (xe(t), a = t.type, e !== null && t.stateNode != null) e.memoizedProps !== r && wc(t);
				else {
					if (!r) {
						if (t.stateNode === null) throw Error(i(166));
						return kc(t), null;
					}
					if (o = me.current, Fi(t)) Ni(t, o);
					else {
						var s = Bd(ge.current);
						switch (o) {
							case 1:
								o = s.createElementNS("http://www.w3.org/2000/svg", a);
								break;
							case 2:
								o = s.createElementNS("http://www.w3.org/1998/Math/MathML", a);
								break;
							default: switch (a) {
								case "svg":
									o = s.createElementNS("http://www.w3.org/2000/svg", a);
									break;
								case "math":
									o = s.createElementNS("http://www.w3.org/1998/Math/MathML", a);
									break;
								case "script":
									o = s.createElement("div"), o.innerHTML = "<script><\/script>", o = o.removeChild(o.firstChild);
									break;
								case "select":
									o = typeof r.is == "string" ? s.createElement("select", { is: r.is }) : s.createElement("select"), r.multiple ? o.multiple = !0 : r.size && (o.size = r.size);
									break;
								default: o = typeof r.is == "string" ? s.createElement(a, { is: r.is }) : s.createElement(a);
							}
						}
						o[mt] = t, o[ht] = r;
						a: for (s = t.child; s !== null;) {
							if (s.tag === 5 || s.tag === 6) o.appendChild(s.stateNode);
							else if (s.tag !== 4 && s.tag !== 27 && s.child !== null) {
								s.child.return = s, s = s.child;
								continue;
							}
							if (s === t) break a;
							for (; s.sibling === null;) {
								if (s.return === null || s.return === t) break a;
								s = s.return;
							}
							s.sibling.return = s.return, s = s.sibling;
						}
						t.stateNode = o;
						a: switch (Pd(o, a, r), a) {
							case "button":
							case "input":
							case "select":
							case "textarea":
								r = !!r.autoFocus;
								break a;
							case "img":
								r = !0;
								break a;
							default: r = !1;
						}
						r && wc(t);
					}
				}
				return kc(t), Tc(t, t.type, e === null ? null : e.memoizedProps, t.pendingProps, n), null;
			case 6:
				if (e && t.stateNode != null) e.memoizedProps !== r && wc(t);
				else {
					if (typeof r != "string" && t.stateNode === null) throw Error(i(166));
					if (e = ge.current, Fi(t)) {
						if (e = t.stateNode, n = t.memoizedProps, r = null, a = Di, a !== null) switch (a.tag) {
							case 27:
							case 5: r = a.memoizedProps;
						}
						e[mt] = t, e = !!(e.nodeValue === n || r !== null && !0 === r.suppressHydrationWarning || jd(e.nodeValue, n)), e || Mi(t, !0);
					} else e = Bd(e).createTextNode(r), e[mt] = t, t.stateNode = e;
				}
				return kc(t), null;
			case 31:
				if (n = t.memoizedState, e === null || e.memoizedState !== null) {
					if (r = Fi(t), n !== null) {
						if (e === null) {
							if (!r) throw Error(i(318));
							if (e = t.memoizedState, e = e === null ? null : e.dehydrated, !e) throw Error(i(557));
							e[mt] = t;
						} else Ii(), !(t.flags & 128) && (t.memoizedState = null), t.flags |= 4;
						kc(t), e = !1;
					} else n = Li(), e !== null && e.memoizedState !== null && (e.memoizedState.hydrationErrors = n), e = !0;
					if (!e) return t.flags & 256 ? (eo(t), t) : (eo(t), null);
					if (t.flags & 128) throw Error(i(558));
				}
				return kc(t), null;
			case 13:
				if (r = t.memoizedState, e === null || e.memoizedState !== null && e.memoizedState.dehydrated !== null) {
					if (a = Fi(t), r !== null && r.dehydrated !== null) {
						if (e === null) {
							if (!a) throw Error(i(318));
							if (a = t.memoizedState, a = a === null ? null : a.dehydrated, !a) throw Error(i(317));
							a[mt] = t;
						} else Ii(), !(t.flags & 128) && (t.memoizedState = null), t.flags |= 4;
						kc(t), a = !1;
					} else a = Li(), e !== null && e.memoizedState !== null && (e.memoizedState.hydrationErrors = a), a = !0;
					if (!a) return t.flags & 256 ? (eo(t), t) : (eo(t), null);
				}
				return eo(t), t.flags & 128 ? (t.lanes = n, t) : (n = r !== null, e = e !== null && e.memoizedState !== null, n && (r = t.child, a = null, r.alternate !== null && r.alternate.memoizedState !== null && r.alternate.memoizedState.cachePool !== null && (a = r.alternate.memoizedState.cachePool.pool), o = null, r.memoizedState !== null && r.memoizedState.cachePool !== null && (o = r.memoizedState.cachePool.pool), o !== a && (r.flags |= 2048)), n !== e && n && (t.child.flags |= 8192), Dc(t, t.updateQueue), kc(t), null);
			case 4: return ye(), e === null && xd(t.stateNode.containerInfo), kc(t), null;
			case 10: return Ui(t.type), kc(t), null;
			case 19:
				if (fe(Y), r = t.memoizedState, r === null) return kc(t), null;
				if (a = (t.flags & 128) != 0, o = r.rendering, o === null) if (a) Oc(r, !1);
				else {
					if (Hl !== 0 || e !== null && e.flags & 128) for (e = t.child; e !== null;) {
						if (o = to(e), o !== null) {
							for (t.flags |= 128, Oc(r, !1), e = o.updateQueue, t.updateQueue = e, Dc(t, e), t.subtreeFlags = 0, e = n, n = t.child; n !== null;) I(n, e), n = n.sibling;
							return pe(Y, Y.current & 1 | 2), ki && Si(t, r.treeForkCount), t.child;
						}
						e = e.sibling;
					}
					r.tail !== null && Pe() > $l && (t.flags |= 128, a = !0, Oc(r, !1), t.lanes = 4194304);
				}
				else {
					if (!a) if (e = to(o), e !== null) {
						if (t.flags |= 128, a = !0, e = e.updateQueue, t.updateQueue = e, Dc(t, e), Oc(r, !0), r.tail === null && r.tailMode === "hidden" && !o.alternate && !ki) return kc(t), null;
					} else 2 * Pe() - r.renderingStartTime > $l && n !== 536870912 && (t.flags |= 128, a = !0, Oc(r, !1), t.lanes = 4194304);
					r.isBackwards ? (o.sibling = t.child, t.child = o) : (e = r.last, e === null ? t.child = o : e.sibling = o, r.last = o);
				}
				return r.tail === null ? (kc(t), null) : (e = r.tail, r.rendering = e, r.tail = e.sibling, r.renderingStartTime = Pe(), e.sibling = null, n = Y.current, pe(Y, a ? n & 1 | 2 : n & 1), ki && Si(t, r.treeForkCount), e);
			case 22:
			case 23: return eo(t), qa(), r = t.memoizedState !== null, e === null ? r && (t.flags |= 8192) : e.memoizedState !== null !== r && (t.flags |= 8192), r ? n & 536870912 && !(t.flags & 128) && (kc(t), t.subtreeFlags & 6 && (t.flags |= 8192)) : kc(t), n = t.updateQueue, n !== null && Dc(t, n.retryQueue), n = null, e !== null && e.memoizedState !== null && e.memoizedState.cachePool !== null && (n = e.memoizedState.cachePool.pool), r = null, t.memoizedState !== null && t.memoizedState.cachePool !== null && (r = t.memoizedState.cachePool.pool), r !== n && (t.flags |= 2048), e !== null && fe(da), null;
			case 24: return n = null, e !== null && (n = e.memoizedState.cache), t.memoizedState.cache !== n && (t.flags |= 2048), Ui(ta), kc(t), null;
			case 25: return null;
			case 30: return null;
		}
		throw Error(i(156, t.tag));
	}
	function jc(e, t) {
		switch (Ti(t), t.tag) {
			case 1: return e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
			case 3: return Ui(ta), ye(), e = t.flags, e & 65536 && !(e & 128) ? (t.flags = e & -65537 | 128, t) : null;
			case 26:
			case 27:
			case 5: return xe(t), null;
			case 31:
				if (t.memoizedState !== null) {
					if (eo(t), t.alternate === null) throw Error(i(340));
					Ii();
				}
				return e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
			case 13:
				if (eo(t), e = t.memoizedState, e !== null && e.dehydrated !== null) {
					if (t.alternate === null) throw Error(i(340));
					Ii();
				}
				return e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
			case 19: return fe(Y), null;
			case 4: return ye(), null;
			case 10: return Ui(t.type), null;
			case 22:
			case 23: return eo(t), qa(), e !== null && fe(da), e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
			case 24: return Ui(ta), null;
			case 25: return null;
			default: return null;
		}
	}
	function Mc(e, t) {
		switch (Ti(t), t.tag) {
			case 3:
				Ui(ta), ye();
				break;
			case 26:
			case 27:
			case 5:
				xe(t);
				break;
			case 4:
				ye();
				break;
			case 31:
				t.memoizedState !== null && eo(t);
				break;
			case 13:
				eo(t);
				break;
			case 19:
				fe(Y);
				break;
			case 10:
				Ui(t.type);
				break;
			case 22:
			case 23:
				eo(t), qa(), e !== null && fe(da);
				break;
			case 24: Ui(ta);
		}
	}
	function Nc(e, t) {
		try {
			var n = t.updateQueue, r = n === null ? null : n.lastEffect;
			if (r !== null) {
				var i = r.next;
				n = i;
				do {
					if ((n.tag & e) === e) {
						r = void 0;
						var a = n.create, o = n.inst;
						r = a(), o.destroy = r;
					}
					n = n.next;
				} while (n !== i);
			}
		} catch (e) {
			Hu(t, t.return, e);
		}
	}
	function Pc(e, t, n) {
		try {
			var r = t.updateQueue, i = r === null ? null : r.lastEffect;
			if (i !== null) {
				var a = i.next;
				r = a;
				do {
					if ((r.tag & e) === e) {
						var o = r.inst, s = o.destroy;
						if (s !== void 0) {
							o.destroy = void 0, i = t;
							var c = n, l = s;
							try {
								l();
							} catch (e) {
								Hu(i, c, e);
							}
						}
					}
					r = r.next;
				} while (r !== a);
			}
		} catch (e) {
			Hu(t, t.return, e);
		}
	}
	function Fc(e) {
		var t = e.updateQueue;
		if (t !== null) {
			var n = e.stateNode;
			try {
				Ua(t, n);
			} catch (t) {
				Hu(e, e.return, t);
			}
		}
	}
	function Ic(e, t, n) {
		n.props = Ls(e.type, e.memoizedProps), n.state = e.memoizedState;
		try {
			n.componentWillUnmount();
		} catch (n) {
			Hu(e, t, n);
		}
	}
	function Lc(e, t) {
		try {
			var n = e.ref;
			if (n !== null) {
				switch (e.tag) {
					case 26:
					case 27:
					case 5:
						var r = e.stateNode;
						break;
					case 30:
						r = e.stateNode;
						break;
					default: r = e.stateNode;
				}
				typeof n == "function" ? e.refCleanup = n(r) : n.current = r;
			}
		} catch (n) {
			Hu(e, t, n);
		}
	}
	function Rc(e, t) {
		var n = e.ref, r = e.refCleanup;
		if (n !== null) if (typeof r == "function") try {
			r();
		} catch (n) {
			Hu(e, t, n);
		} finally {
			e.refCleanup = null, e = e.alternate, e != null && (e.refCleanup = null);
		}
		else if (typeof n == "function") try {
			n(null);
		} catch (n) {
			Hu(e, t, n);
		}
		else n.current = null;
	}
	function zc(e) {
		var t = e.type, n = e.memoizedProps, r = e.stateNode;
		try {
			a: switch (t) {
				case "button":
				case "input":
				case "select":
				case "textarea":
					n.autoFocus && r.focus();
					break a;
				case "img": n.src ? r.src = n.src : n.srcSet && (r.srcset = n.srcSet);
			}
		} catch (t) {
			Hu(e, e.return, t);
		}
	}
	function Bc(e, t, n) {
		try {
			var r = e.stateNode;
			Fd(r, e.type, n, t), r[ht] = t;
		} catch (t) {
			Hu(e, e.return, t);
		}
	}
	function Vc(e) {
		return e.tag === 5 || e.tag === 3 || e.tag === 26 || e.tag === 27 && Zd(e.type) || e.tag === 4;
	}
	function Hc(e) {
		a: for (;;) {
			for (; e.sibling === null;) {
				if (e.return === null || Vc(e.return)) return null;
				e = e.return;
			}
			for (e.sibling.return = e.return, e = e.sibling; e.tag !== 5 && e.tag !== 6 && e.tag !== 18;) {
				if (e.tag === 27 && Zd(e.type) || e.flags & 2 || e.child === null || e.tag === 4) continue a;
				e.child.return = e, e = e.child;
			}
			if (!(e.flags & 2)) return e.stateNode;
		}
	}
	function Uc(e, t, n) {
		var r = e.tag;
		if (r === 5 || r === 6) e = e.stateNode, t ? (n.nodeType === 9 ? n.body : n.nodeName === "HTML" ? n.ownerDocument.body : n).insertBefore(e, t) : (t = n.nodeType === 9 ? n.body : n.nodeName === "HTML" ? n.ownerDocument.body : n, t.appendChild(e), n = n._reactRootContainer, n != null || t.onclick !== null || (t.onclick = on));
		else if (r !== 4 && (r === 27 && Zd(e.type) && (n = e.stateNode, t = null), e = e.child, e !== null)) for (Uc(e, t, n), e = e.sibling; e !== null;) Uc(e, t, n), e = e.sibling;
	}
	function Wc(e, t, n) {
		var r = e.tag;
		if (r === 5 || r === 6) e = e.stateNode, t ? n.insertBefore(e, t) : n.appendChild(e);
		else if (r !== 4 && (r === 27 && Zd(e.type) && (n = e.stateNode), e = e.child, e !== null)) for (Wc(e, t, n), e = e.sibling; e !== null;) Wc(e, t, n), e = e.sibling;
	}
	function Gc(e) {
		var t = e.stateNode, n = e.memoizedProps;
		try {
			for (var r = e.type, i = t.attributes; i.length;) t.removeAttributeNode(i[0]);
			Pd(t, r, n), t[mt] = e, t[ht] = n;
		} catch (t) {
			Hu(e, e.return, t);
		}
	}
	var Kc = !1, qc = !1, Jc = !1, Yc = typeof WeakSet == "function" ? WeakSet : Set, Xc = null;
	function Zc(e, t) {
		if (e = e.containerInfo, Rd = sp, e = jr(e), Mr(e)) {
			if ("selectionStart" in e) var n = {
				start: e.selectionStart,
				end: e.selectionEnd
			};
			else a: {
				n = (n = e.ownerDocument) && n.defaultView || window;
				var r = n.getSelection && n.getSelection();
				if (r && r.rangeCount !== 0) {
					n = r.anchorNode;
					var a = r.anchorOffset, o = r.focusNode;
					r = r.focusOffset;
					try {
						n.nodeType, o.nodeType;
					} catch {
						n = null;
						break a;
					}
					var s = 0, c = -1, l = -1, u = 0, d = 0, f = e, p = null;
					b: for (;;) {
						for (var m; f !== n || a !== 0 && f.nodeType !== 3 || (c = s + a), f !== o || r !== 0 && f.nodeType !== 3 || (l = s + r), f.nodeType === 3 && (s += f.nodeValue.length), (m = f.firstChild) !== null;) p = f, f = m;
						for (;;) {
							if (f === e) break b;
							if (p === n && ++u === a && (c = s), p === o && ++d === r && (l = s), (m = f.nextSibling) !== null) break;
							f = p, p = f.parentNode;
						}
						f = m;
					}
					n = c === -1 || l === -1 ? null : {
						start: c,
						end: l
					};
				} else n = null;
			}
			n ||= {
				start: 0,
				end: 0
			};
		} else n = null;
		for (zd = {
			focusedElem: e,
			selectionRange: n
		}, sp = !1, Xc = t; Xc !== null;) if (t = Xc, e = t.child, t.subtreeFlags & 1028 && e !== null) e.return = t, Xc = e;
		else for (; Xc !== null;) {
			switch (t = Xc, o = t.alternate, e = t.flags, t.tag) {
				case 0:
					if (e & 4 && (e = t.updateQueue, e = e === null ? null : e.events, e !== null)) for (n = 0; n < e.length; n++) a = e[n], a.ref.impl = a.nextImpl;
					break;
				case 11:
				case 15: break;
				case 1:
					if (e & 1024 && o !== null) {
						e = void 0, n = t, a = o.memoizedProps, o = o.memoizedState, r = n.stateNode;
						try {
							var h = Ls(n.type, a);
							e = r.getSnapshotBeforeUpdate(h, o), r.__reactInternalSnapshotBeforeUpdate = e;
						} catch (e) {
							Hu(n, n.return, e);
						}
					}
					break;
				case 3:
					if (e & 1024) {
						if (e = t.stateNode.containerInfo, n = e.nodeType, n === 9) ef(e);
						else if (n === 1) switch (e.nodeName) {
							case "HEAD":
							case "HTML":
							case "BODY":
								ef(e);
								break;
							default: e.textContent = "";
						}
					}
					break;
				case 5:
				case 26:
				case 27:
				case 6:
				case 4:
				case 17: break;
				default: if (e & 1024) throw Error(i(163));
			}
			if (e = t.sibling, e !== null) {
				e.return = t.return, Xc = e;
				break;
			}
			Xc = t.return;
		}
	}
	function Qc(e, t, n) {
		var r = n.flags;
		switch (n.tag) {
			case 0:
			case 11:
			case 15:
				pl(e, n), r & 4 && Nc(5, n);
				break;
			case 1:
				if (pl(e, n), r & 4) if (e = n.stateNode, t === null) try {
					e.componentDidMount();
				} catch (e) {
					Hu(n, n.return, e);
				}
				else {
					var i = Ls(n.type, t.memoizedProps);
					t = t.memoizedState;
					try {
						e.componentDidUpdate(i, t, e.__reactInternalSnapshotBeforeUpdate);
					} catch (e) {
						Hu(n, n.return, e);
					}
				}
				r & 64 && Fc(n), r & 512 && Lc(n, n.return);
				break;
			case 3:
				if (pl(e, n), r & 64 && (e = n.updateQueue, e !== null)) {
					if (t = null, n.child !== null) switch (n.child.tag) {
						case 27:
						case 5:
							t = n.child.stateNode;
							break;
						case 1: t = n.child.stateNode;
					}
					try {
						Ua(e, t);
					} catch (e) {
						Hu(n, n.return, e);
					}
				}
				break;
			case 27: t === null && r & 4 && Gc(n);
			case 26:
			case 5:
				pl(e, n), t === null && r & 4 && zc(n), r & 512 && Lc(n, n.return);
				break;
			case 12:
				pl(e, n);
				break;
			case 31:
				pl(e, n), r & 4 && il(e, n);
				break;
			case 13:
				pl(e, n), r & 4 && al(e, n), r & 64 && (e = n.memoizedState, e !== null && (e = e.dehydrated, e !== null && (n = Ku.bind(null, n), sf(e, n))));
				break;
			case 22:
				if (r = n.memoizedState !== null || Kc, !r) {
					t = t !== null && t.memoizedState !== null || qc, i = Kc;
					var a = qc;
					Kc = r, (qc = t) && !a ? hl(e, n, (n.subtreeFlags & 8772) != 0) : pl(e, n), Kc = i, qc = a;
				}
				break;
			case 30: break;
			default: pl(e, n);
		}
	}
	function $c(e) {
		var t = e.alternate;
		t !== null && (e.alternate = null, $c(t)), e.child = null, e.deletions = null, e.sibling = null, e.tag === 5 && (t = e.stateNode, t !== null && St(t)), e.stateNode = null, e.return = null, e.dependencies = null, e.memoizedProps = null, e.memoizedState = null, e.pendingProps = null, e.stateNode = null, e.updateQueue = null;
	}
	var el = null, tl = !1;
	function nl(e, t, n) {
		for (n = n.child; n !== null;) rl(e, t, n), n = n.sibling;
	}
	function rl(e, t, n) {
		if (Ue && typeof Ue.onCommitFiberUnmount == "function") try {
			Ue.onCommitFiberUnmount(He, n);
		} catch {}
		switch (n.tag) {
			case 26:
				qc || Rc(n, t), nl(e, t, n), n.memoizedState ? n.memoizedState.count-- : n.stateNode && (n = n.stateNode, n.parentNode.removeChild(n));
				break;
			case 27:
				qc || Rc(n, t);
				var r = el, i = tl;
				Zd(n.type) && (el = n.stateNode, tl = !1), nl(e, t, n), pf(n.stateNode), el = r, tl = i;
				break;
			case 5: qc || Rc(n, t);
			case 6:
				if (r = el, i = tl, el = null, nl(e, t, n), el = r, tl = i, el !== null) if (tl) try {
					(el.nodeType === 9 ? el.body : el.nodeName === "HTML" ? el.ownerDocument.body : el).removeChild(n.stateNode);
				} catch (e) {
					Hu(n, t, e);
				}
				else try {
					el.removeChild(n.stateNode);
				} catch (e) {
					Hu(n, t, e);
				}
				break;
			case 18:
				el !== null && (tl ? (e = el, Qd(e.nodeType === 9 ? e.body : e.nodeName === "HTML" ? e.ownerDocument.body : e, n.stateNode), Np(e)) : Qd(el, n.stateNode));
				break;
			case 4:
				r = el, i = tl, el = n.stateNode.containerInfo, tl = !0, nl(e, t, n), el = r, tl = i;
				break;
			case 0:
			case 11:
			case 14:
			case 15:
				Pc(2, n, t), qc || Pc(4, n, t), nl(e, t, n);
				break;
			case 1:
				qc || (Rc(n, t), r = n.stateNode, typeof r.componentWillUnmount == "function" && Ic(n, t, r)), nl(e, t, n);
				break;
			case 21:
				nl(e, t, n);
				break;
			case 22:
				qc = (r = qc) || n.memoizedState !== null, nl(e, t, n), qc = r;
				break;
			default: nl(e, t, n);
		}
	}
	function il(e, t) {
		if (t.memoizedState === null && (e = t.alternate, e !== null && (e = e.memoizedState, e !== null))) {
			e = e.dehydrated;
			try {
				Np(e);
			} catch (e) {
				Hu(t, t.return, e);
			}
		}
	}
	function al(e, t) {
		if (t.memoizedState === null && (e = t.alternate, e !== null && (e = e.memoizedState, e !== null && (e = e.dehydrated, e !== null)))) try {
			Np(e);
		} catch (e) {
			Hu(t, t.return, e);
		}
	}
	function ol(e) {
		switch (e.tag) {
			case 31:
			case 13:
			case 19:
				var t = e.stateNode;
				return t === null && (t = e.stateNode = new Yc()), t;
			case 22: return e = e.stateNode, t = e._retryCache, t === null && (t = e._retryCache = new Yc()), t;
			default: throw Error(i(435, e.tag));
		}
	}
	function sl(e, t) {
		var n = ol(e);
		t.forEach(function(t) {
			if (!n.has(t)) {
				n.add(t);
				var r = qu.bind(null, e, t);
				t.then(r, r);
			}
		});
	}
	function cl(e, t) {
		var n = t.deletions;
		if (n !== null) for (var r = 0; r < n.length; r++) {
			var a = n[r], o = e, s = t, c = s;
			a: for (; c !== null;) {
				switch (c.tag) {
					case 27:
						if (Zd(c.type)) {
							el = c.stateNode, tl = !1;
							break a;
						}
						break;
					case 5:
						el = c.stateNode, tl = !1;
						break a;
					case 3:
					case 4:
						el = c.stateNode.containerInfo, tl = !0;
						break a;
				}
				c = c.return;
			}
			if (el === null) throw Error(i(160));
			rl(o, s, a), el = null, tl = !1, o = a.alternate, o !== null && (o.return = null), a.return = null;
		}
		if (t.subtreeFlags & 13886) for (t = t.child; t !== null;) ul(t, e), t = t.sibling;
	}
	var ll = null;
	function ul(e, t) {
		var n = e.alternate, r = e.flags;
		switch (e.tag) {
			case 0:
			case 11:
			case 14:
			case 15:
				cl(t, e), dl(e), r & 4 && (Pc(3, e, e.return), Nc(3, e), Pc(5, e, e.return));
				break;
			case 1:
				cl(t, e), dl(e), r & 512 && (qc || n === null || Rc(n, n.return)), r & 64 && Kc && (e = e.updateQueue, e !== null && (r = e.callbacks, r !== null && (n = e.shared.hiddenCallbacks, e.shared.hiddenCallbacks = n === null ? r : n.concat(r))));
				break;
			case 26:
				var a = ll;
				if (cl(t, e), dl(e), r & 512 && (qc || n === null || Rc(n, n.return)), r & 4) {
					var o = n === null ? null : n.memoizedState;
					if (r = e.memoizedState, n === null) if (r === null) if (e.stateNode === null) {
						a: {
							r = e.type, n = e.memoizedProps, a = a.ownerDocument || a;
							b: switch (r) {
								case "title":
									o = a.getElementsByTagName("title")[0], (!o || o[xt] || o[mt] || o.namespaceURI === "http://www.w3.org/2000/svg" || o.hasAttribute("itemprop")) && (o = a.createElement(r), a.head.insertBefore(o, a.querySelector("head > title"))), Pd(o, r, n), o[mt] = e, Dt(o), r = o;
									break a;
								case "link":
									var s = Vf("link", "href", a).get(r + (n.href || ""));
									if (s) {
										for (var c = 0; c < s.length; c++) if (o = s[c], o.getAttribute("href") === (n.href == null || n.href === "" ? null : n.href) && o.getAttribute("rel") === (n.rel == null ? null : n.rel) && o.getAttribute("title") === (n.title == null ? null : n.title) && o.getAttribute("crossorigin") === (n.crossOrigin == null ? null : n.crossOrigin)) {
											s.splice(c, 1);
											break b;
										}
									}
									o = a.createElement(r), Pd(o, r, n), a.head.appendChild(o);
									break;
								case "meta":
									if (s = Vf("meta", "content", a).get(r + (n.content || ""))) {
										for (c = 0; c < s.length; c++) if (o = s[c], o.getAttribute("content") === (n.content == null ? null : "" + n.content) && o.getAttribute("name") === (n.name == null ? null : n.name) && o.getAttribute("property") === (n.property == null ? null : n.property) && o.getAttribute("http-equiv") === (n.httpEquiv == null ? null : n.httpEquiv) && o.getAttribute("charset") === (n.charSet == null ? null : n.charSet)) {
											s.splice(c, 1);
											break b;
										}
									}
									o = a.createElement(r), Pd(o, r, n), a.head.appendChild(o);
									break;
								default: throw Error(i(468, r));
							}
							o[mt] = e, Dt(o), r = o;
						}
						e.stateNode = r;
					} else Hf(a, e.type, e.stateNode);
					else e.stateNode = If(a, r, e.memoizedProps);
					else o === r ? r === null && e.stateNode !== null && Bc(e, e.memoizedProps, n.memoizedProps) : (o === null ? n.stateNode !== null && (n = n.stateNode, n.parentNode.removeChild(n)) : o.count--, r === null ? Hf(a, e.type, e.stateNode) : If(a, r, e.memoizedProps));
				}
				break;
			case 27:
				cl(t, e), dl(e), r & 512 && (qc || n === null || Rc(n, n.return)), n !== null && r & 4 && Bc(e, e.memoizedProps, n.memoizedProps);
				break;
			case 5:
				if (cl(t, e), dl(e), r & 512 && (qc || n === null || Rc(n, n.return)), e.flags & 32) {
					a = e.stateNode;
					try {
						Zt(a, "");
					} catch (t) {
						Hu(e, e.return, t);
					}
				}
				r & 4 && e.stateNode != null && (a = e.memoizedProps, Bc(e, a, n === null ? a : n.memoizedProps)), r & 1024 && (Jc = !0);
				break;
			case 6:
				if (cl(t, e), dl(e), r & 4) {
					if (e.stateNode === null) throw Error(i(162));
					r = e.memoizedProps, n = e.stateNode;
					try {
						n.nodeValue = r;
					} catch (t) {
						Hu(e, e.return, t);
					}
				}
				break;
			case 3:
				if (Bf = null, a = ll, ll = gf(t.containerInfo), cl(t, e), ll = a, dl(e), r & 4 && n !== null && n.memoizedState.isDehydrated) try {
					Np(t.containerInfo);
				} catch (t) {
					Hu(e, e.return, t);
				}
				Jc && (Jc = !1, fl(e));
				break;
			case 4:
				r = ll, ll = gf(e.stateNode.containerInfo), cl(t, e), dl(e), ll = r;
				break;
			case 12:
				cl(t, e), dl(e);
				break;
			case 31:
				cl(t, e), dl(e), r & 4 && (r = e.updateQueue, r !== null && (e.updateQueue = null, sl(e, r)));
				break;
			case 13:
				cl(t, e), dl(e), e.child.flags & 8192 && e.memoizedState !== null != (n !== null && n.memoizedState !== null) && (Zl = Pe()), r & 4 && (r = e.updateQueue, r !== null && (e.updateQueue = null, sl(e, r)));
				break;
			case 22:
				a = e.memoizedState !== null;
				var l = n !== null && n.memoizedState !== null, u = Kc, d = qc;
				if (Kc = u || a, qc = d || l, cl(t, e), qc = d, Kc = u, dl(e), r & 8192) a: for (t = e.stateNode, t._visibility = a ? t._visibility & -2 : t._visibility | 1, a && (n === null || l || Kc || qc || ml(e)), n = null, t = e;;) {
					if (t.tag === 5 || t.tag === 26) {
						if (n === null) {
							l = n = t;
							try {
								if (o = l.stateNode, a) s = o.style, typeof s.setProperty == "function" ? s.setProperty("display", "none", "important") : s.display = "none";
								else {
									c = l.stateNode;
									var f = l.memoizedProps.style, p = f != null && f.hasOwnProperty("display") ? f.display : null;
									c.style.display = p == null || typeof p == "boolean" ? "" : ("" + p).trim();
								}
							} catch (e) {
								Hu(l, l.return, e);
							}
						}
					} else if (t.tag === 6) {
						if (n === null) {
							l = t;
							try {
								l.stateNode.nodeValue = a ? "" : l.memoizedProps;
							} catch (e) {
								Hu(l, l.return, e);
							}
						}
					} else if (t.tag === 18) {
						if (n === null) {
							l = t;
							try {
								var m = l.stateNode;
								a ? $d(m, !0) : $d(l.stateNode, !1);
							} catch (e) {
								Hu(l, l.return, e);
							}
						}
					} else if ((t.tag !== 22 && t.tag !== 23 || t.memoizedState === null || t === e) && t.child !== null) {
						t.child.return = t, t = t.child;
						continue;
					}
					if (t === e) break a;
					for (; t.sibling === null;) {
						if (t.return === null || t.return === e) break a;
						n === t && (n = null), t = t.return;
					}
					n === t && (n = null), t.sibling.return = t.return, t = t.sibling;
				}
				r & 4 && (r = e.updateQueue, r !== null && (n = r.retryQueue, n !== null && (r.retryQueue = null, sl(e, n))));
				break;
			case 19:
				cl(t, e), dl(e), r & 4 && (r = e.updateQueue, r !== null && (e.updateQueue = null, sl(e, r)));
				break;
			case 30: break;
			case 21: break;
			default: cl(t, e), dl(e);
		}
	}
	function dl(e) {
		var t = e.flags;
		if (t & 2) {
			try {
				for (var n, r = e.return; r !== null;) {
					if (Vc(r)) {
						n = r;
						break;
					}
					r = r.return;
				}
				if (n == null) throw Error(i(160));
				switch (n.tag) {
					case 27:
						var a = n.stateNode;
						Wc(e, Hc(e), a);
						break;
					case 5:
						var o = n.stateNode;
						n.flags & 32 && (Zt(o, ""), n.flags &= -33), Wc(e, Hc(e), o);
						break;
					case 3:
					case 4:
						var s = n.stateNode.containerInfo;
						Uc(e, Hc(e), s);
						break;
					default: throw Error(i(161));
				}
			} catch (t) {
				Hu(e, e.return, t);
			}
			e.flags &= -3;
		}
		t & 4096 && (e.flags &= -4097);
	}
	function fl(e) {
		if (e.subtreeFlags & 1024) for (e = e.child; e !== null;) {
			var t = e;
			fl(t), t.tag === 5 && t.flags & 1024 && t.stateNode.reset(), e = e.sibling;
		}
	}
	function pl(e, t) {
		if (t.subtreeFlags & 8772) for (t = t.child; t !== null;) Qc(e, t.alternate, t), t = t.sibling;
	}
	function ml(e) {
		for (e = e.child; e !== null;) {
			var t = e;
			switch (t.tag) {
				case 0:
				case 11:
				case 14:
				case 15:
					Pc(4, t, t.return), ml(t);
					break;
				case 1:
					Rc(t, t.return);
					var n = t.stateNode;
					typeof n.componentWillUnmount == "function" && Ic(t, t.return, n), ml(t);
					break;
				case 27: pf(t.stateNode);
				case 26:
				case 5:
					Rc(t, t.return), ml(t);
					break;
				case 22:
					t.memoizedState === null && ml(t);
					break;
				case 30:
					ml(t);
					break;
				default: ml(t);
			}
			e = e.sibling;
		}
	}
	function hl(e, t, n) {
		for (n &&= (t.subtreeFlags & 8772) != 0, t = t.child; t !== null;) {
			var r = t.alternate, i = e, a = t, o = a.flags;
			switch (a.tag) {
				case 0:
				case 11:
				case 15:
					hl(i, a, n), Nc(4, a);
					break;
				case 1:
					if (hl(i, a, n), r = a, i = r.stateNode, typeof i.componentDidMount == "function") try {
						i.componentDidMount();
					} catch (e) {
						Hu(r, r.return, e);
					}
					if (r = a, i = r.updateQueue, i !== null) {
						var s = r.stateNode;
						try {
							var c = i.shared.hiddenCallbacks;
							if (c !== null) for (i.shared.hiddenCallbacks = null, i = 0; i < c.length; i++) Ha(c[i], s);
						} catch (e) {
							Hu(r, r.return, e);
						}
					}
					n && o & 64 && Fc(a), Lc(a, a.return);
					break;
				case 27: Gc(a);
				case 26:
				case 5:
					hl(i, a, n), n && r === null && o & 4 && zc(a), Lc(a, a.return);
					break;
				case 12:
					hl(i, a, n);
					break;
				case 31:
					hl(i, a, n), n && o & 4 && il(i, a);
					break;
				case 13:
					hl(i, a, n), n && o & 4 && al(i, a);
					break;
				case 22:
					a.memoizedState === null && hl(i, a, n), Lc(a, a.return);
					break;
				case 30: break;
				default: hl(i, a, n);
			}
			t = t.sibling;
		}
	}
	function gl(e, t) {
		var n = null;
		e !== null && e.memoizedState !== null && e.memoizedState.cachePool !== null && (n = e.memoizedState.cachePool.pool), e = null, t.memoizedState !== null && t.memoizedState.cachePool !== null && (e = t.memoizedState.cachePool.pool), e !== n && (e != null && e.refCount++, n != null && ra(n));
	}
	function _l(e, t) {
		e = null, t.alternate !== null && (e = t.alternate.memoizedState.cache), t = t.memoizedState.cache, t !== e && (t.refCount++, e != null && ra(e));
	}
	function vl(e, t, n, r) {
		if (t.subtreeFlags & 10256) for (t = t.child; t !== null;) yl(e, t, n, r), t = t.sibling;
	}
	function yl(e, t, n, r) {
		var i = t.flags;
		switch (t.tag) {
			case 0:
			case 11:
			case 15:
				vl(e, t, n, r), i & 2048 && Nc(9, t);
				break;
			case 1:
				vl(e, t, n, r);
				break;
			case 3:
				vl(e, t, n, r), i & 2048 && (e = null, t.alternate !== null && (e = t.alternate.memoizedState.cache), t = t.memoizedState.cache, t !== e && (t.refCount++, e != null && ra(e)));
				break;
			case 12:
				if (i & 2048) {
					vl(e, t, n, r), e = t.stateNode;
					try {
						var a = t.memoizedProps, o = a.id, s = a.onPostCommit;
						typeof s == "function" && s(o, t.alternate === null ? "mount" : "update", e.passiveEffectDuration, -0);
					} catch (e) {
						Hu(t, t.return, e);
					}
				} else vl(e, t, n, r);
				break;
			case 31:
				vl(e, t, n, r);
				break;
			case 13:
				vl(e, t, n, r);
				break;
			case 23: break;
			case 22:
				a = t.stateNode, o = t.alternate, t.memoizedState === null ? a._visibility & 2 ? vl(e, t, n, r) : (a._visibility |= 2, bl(e, t, n, r, (t.subtreeFlags & 10256) != 0 || !1)) : a._visibility & 2 ? vl(e, t, n, r) : xl(e, t), i & 2048 && gl(o, t);
				break;
			case 24:
				vl(e, t, n, r), i & 2048 && _l(t.alternate, t);
				break;
			default: vl(e, t, n, r);
		}
	}
	function bl(e, t, n, r, i) {
		for (i &&= (t.subtreeFlags & 10256) != 0 || !1, t = t.child; t !== null;) {
			var a = e, o = t, s = n, c = r, l = o.flags;
			switch (o.tag) {
				case 0:
				case 11:
				case 15:
					bl(a, o, s, c, i), Nc(8, o);
					break;
				case 23: break;
				case 22:
					var u = o.stateNode;
					o.memoizedState === null ? (u._visibility |= 2, bl(a, o, s, c, i)) : u._visibility & 2 ? bl(a, o, s, c, i) : xl(a, o), i && l & 2048 && gl(o.alternate, o);
					break;
				case 24:
					bl(a, o, s, c, i), i && l & 2048 && _l(o.alternate, o);
					break;
				default: bl(a, o, s, c, i);
			}
			t = t.sibling;
		}
	}
	function xl(e, t) {
		if (t.subtreeFlags & 10256) for (t = t.child; t !== null;) {
			var n = e, r = t, i = r.flags;
			switch (r.tag) {
				case 22:
					xl(n, r), i & 2048 && gl(r.alternate, r);
					break;
				case 24:
					xl(n, r), i & 2048 && _l(r.alternate, r);
					break;
				default: xl(n, r);
			}
			t = t.sibling;
		}
	}
	var Sl = 8192;
	function Cl(e, t, n) {
		if (e.subtreeFlags & Sl) for (e = e.child; e !== null;) wl(e, t, n), e = e.sibling;
	}
	function wl(e, t, n) {
		switch (e.tag) {
			case 26:
				Cl(e, t, n), e.flags & Sl && e.memoizedState !== null && Gf(n, ll, e.memoizedState, e.memoizedProps);
				break;
			case 5:
				Cl(e, t, n);
				break;
			case 3:
			case 4:
				var r = ll;
				ll = gf(e.stateNode.containerInfo), Cl(e, t, n), ll = r;
				break;
			case 22:
				e.memoizedState === null && (r = e.alternate, r !== null && r.memoizedState !== null ? (r = Sl, Sl = 16777216, Cl(e, t, n), Sl = r) : Cl(e, t, n));
				break;
			default: Cl(e, t, n);
		}
	}
	function Tl(e) {
		var t = e.alternate;
		if (t !== null && (e = t.child, e !== null)) {
			t.child = null;
			do
				t = e.sibling, e.sibling = null, e = t;
			while (e !== null);
		}
	}
	function El(e) {
		var t = e.deletions;
		if (e.flags & 16) {
			if (t !== null) for (var n = 0; n < t.length; n++) {
				var r = t[n];
				Xc = r, kl(r, e);
			}
			Tl(e);
		}
		if (e.subtreeFlags & 10256) for (e = e.child; e !== null;) Dl(e), e = e.sibling;
	}
	function Dl(e) {
		switch (e.tag) {
			case 0:
			case 11:
			case 15:
				El(e), e.flags & 2048 && Pc(9, e, e.return);
				break;
			case 3:
				El(e);
				break;
			case 12:
				El(e);
				break;
			case 22:
				var t = e.stateNode;
				e.memoizedState !== null && t._visibility & 2 && (e.return === null || e.return.tag !== 13) ? (t._visibility &= -3, Ol(e)) : El(e);
				break;
			default: El(e);
		}
	}
	function Ol(e) {
		var t = e.deletions;
		if (e.flags & 16) {
			if (t !== null) for (var n = 0; n < t.length; n++) {
				var r = t[n];
				Xc = r, kl(r, e);
			}
			Tl(e);
		}
		for (e = e.child; e !== null;) {
			switch (t = e, t.tag) {
				case 0:
				case 11:
				case 15:
					Pc(8, t, t.return), Ol(t);
					break;
				case 22:
					n = t.stateNode, n._visibility & 2 && (n._visibility &= -3, Ol(t));
					break;
				default: Ol(t);
			}
			e = e.sibling;
		}
	}
	function kl(e, t) {
		for (; Xc !== null;) {
			var n = Xc;
			switch (n.tag) {
				case 0:
				case 11:
				case 15:
					Pc(8, n, t);
					break;
				case 23:
				case 22:
					if (n.memoizedState !== null && n.memoizedState.cachePool !== null) {
						var r = n.memoizedState.cachePool.pool;
						r != null && r.refCount++;
					}
					break;
				case 24: ra(n.memoizedState.cache);
			}
			if (r = n.child, r !== null) r.return = n, Xc = r;
			else a: for (n = e; Xc !== null;) {
				r = Xc;
				var i = r.sibling, a = r.return;
				if ($c(r), r === n) {
					Xc = null;
					break a;
				}
				if (i !== null) {
					i.return = a, Xc = i;
					break a;
				}
				Xc = a;
			}
		}
	}
	var Al = {
		getCacheForType: function(e) {
			var t = Yi(ta), n = t.data.get(e);
			return n === void 0 && (n = e(), t.data.set(e, n)), n;
		},
		cacheSignal: function() {
			return Yi(ta).controller.signal;
		}
	}, jl = typeof WeakMap == "function" ? WeakMap : Map, Ml = 0, Nl = null, Pl = null, Fl = 0, Il = 0, Ll = null, Rl = !1, zl = !1, Bl = !1, Vl = 0, Hl = 0, Ul = 0, Wl = 0, Gl = 0, Kl = 0, ql = 0, Jl = null, Yl = null, Xl = !1, Zl = 0, Ql = 0, $l = Infinity, eu = null, tu = null, nu = 0, ru = null, iu = null, au = 0, ou = 0, su = null, cu = null, lu = 0, uu = null;
	function du() {
		return Ml & 2 && Fl !== 0 ? Fl & -Fl : O.T === null ? dt() : ld();
	}
	function fu() {
		if (Kl === 0) if (!(Fl & 536870912) || ki) {
			var e = Xe;
			Xe <<= 1, !(Xe & 3932160) && (Xe = 262144), Kl = e;
		} else Kl = 536870912;
		return e = Ja.current, e !== null && (e.flags |= 32), Kl;
	}
	function pu(e, t, n) {
		(e === Nl && (Il === 2 || Il === 9) || e.cancelPendingCommit !== null) && (bu(e, 0), _u(e, Fl, Kl, !1)), it(e, n), (!(Ml & 2) || e !== Nl) && (e === Nl && (!(Ml & 2) && (Wl |= n), Hl === 4 && _u(e, Fl, Kl, !1)), td(e));
	}
	function mu(e, t, n) {
		if (Ml & 6) throw Error(i(327));
		var r = !n && (t & 127) == 0 && (t & e.expiredLanes) === 0 || et(e, t), a = r ? Du(e, t) : Tu(e, t, !0), o = r;
		do {
			if (a === 0) {
				zl && !r && _u(e, t, 0, !1);
				break;
			} else {
				if (n = e.current.alternate, o && !gu(n)) {
					a = Tu(e, t, !1), o = !1;
					continue;
				}
				if (a === 2) {
					if (o = t, e.errorRecoveryDisabledLanes & o) var s = 0;
					else s = e.pendingLanes & -536870913, s = s === 0 ? s & 536870912 ? 536870912 : 0 : s;
					if (s !== 0) {
						t = s;
						a: {
							var c = e;
							a = Jl;
							var l = c.current.memoizedState.isDehydrated;
							if (l && (bu(c, s).flags |= 256), s = Tu(c, s, !1), s !== 2) {
								if (Bl && !l) {
									c.errorRecoveryDisabledLanes |= o, Wl |= o, a = 4;
									break a;
								}
								o = Yl, Yl = a, o !== null && (Yl === null ? Yl = o : Yl.push.apply(Yl, o));
							}
							a = s;
						}
						if (o = !1, a !== 2) continue;
					}
				}
				if (a === 1) {
					bu(e, 0), _u(e, t, 0, !0);
					break;
				}
				a: {
					switch (r = e, o = a, o) {
						case 0:
						case 1: throw Error(i(345));
						case 4: if ((t & 4194048) !== t) break;
						case 6:
							_u(r, t, Kl, !Rl);
							break a;
						case 2:
							Yl = null;
							break;
						case 3:
						case 5: break;
						default: throw Error(i(329));
					}
					if ((t & 62914560) === t && (a = Zl + 300 - Pe(), 10 < a)) {
						if (_u(r, t, Kl, !Rl), $e(r, 0, !0) !== 0) break a;
						au = t, r.timeoutHandle = Kd(hu.bind(null, r, n, Yl, eu, Xl, t, Kl, Wl, ql, Rl, o, "Throttled", -0, 0), a);
						break a;
					}
					hu(r, n, Yl, eu, Xl, t, Kl, Wl, ql, Rl, o, null, -0, 0);
				}
			}
			break;
		} while (1);
		td(e);
	}
	function hu(e, t, n, r, i, a, o, s, c, l, u, d, f, p) {
		if (e.timeoutHandle = -1, d = t.subtreeFlags, d & 8192 || (d & 16785408) == 16785408) {
			d = {
				stylesheets: null,
				count: 0,
				imgCount: 0,
				imgBytes: 0,
				suspenseyImages: [],
				waitingForImages: !0,
				waitingForViewTransition: !1,
				unsuspend: on
			}, wl(t, a, d);
			var m = (a & 62914560) === a ? Zl - Pe() : (a & 4194048) === a ? Ql - Pe() : 0;
			if (m = qf(d, m), m !== null) {
				au = a, e.cancelPendingCommit = m(Pu.bind(null, e, t, a, n, r, i, o, s, c, u, d, null, f, p)), _u(e, a, o, !l);
				return;
			}
		}
		Pu(e, t, a, n, r, i, o, s, c);
	}
	function gu(e) {
		for (var t = e;;) {
			var n = t.tag;
			if ((n === 0 || n === 11 || n === 15) && t.flags & 16384 && (n = t.updateQueue, n !== null && (n = n.stores, n !== null))) for (var r = 0; r < n.length; r++) {
				var i = n[r], a = i.getSnapshot;
				i = i.value;
				try {
					if (!Er(a(), i)) return !1;
				} catch {
					return !1;
				}
			}
			if (n = t.child, t.subtreeFlags & 16384 && n !== null) n.return = t, t = n;
			else {
				if (t === e) break;
				for (; t.sibling === null;) {
					if (t.return === null || t.return === e) return !0;
					t = t.return;
				}
				t.sibling.return = t.return, t = t.sibling;
			}
		}
		return !0;
	}
	function _u(e, t, n, r) {
		t &= ~Gl, t &= ~Wl, e.suspendedLanes |= t, e.pingedLanes &= ~t, r && (e.warmLanes |= t), r = e.expirationTimes;
		for (var i = t; 0 < i;) {
			var a = 31 - Ge(i), o = 1 << a;
			r[a] = -1, i &= ~o;
		}
		n !== 0 && ot(e, n, t);
	}
	function vu() {
		return Ml & 6 ? !0 : (nd(0, !1), !1);
	}
	function yu() {
		if (Pl !== null) {
			if (Il === 0) var e = Pl.return;
			else e = Pl, Vi = Bi = null, bo(e), Ta = null, Ea = 0, e = Pl;
			for (; e !== null;) Mc(e.alternate, e), e = e.return;
			Pl = null;
		}
	}
	function bu(e, t) {
		var n = e.timeoutHandle;
		n !== -1 && (e.timeoutHandle = -1, qd(n)), n = e.cancelPendingCommit, n !== null && (e.cancelPendingCommit = null, n()), au = 0, yu(), Nl = e, Pl = n = pi(e.current, null), Fl = t, Il = 0, Ll = null, Rl = !1, zl = et(e, t), Bl = !1, ql = Kl = Gl = Wl = Ul = Hl = 0, Yl = Jl = null, Xl = !1, t & 8 && (t |= t & 32);
		var r = e.entangledLanes;
		if (r !== 0) for (e = e.entanglements, r &= t; 0 < r;) {
			var i = 31 - Ge(r), a = 1 << i;
			t |= e[i], r &= ~a;
		}
		return Vl = t, ri(), n;
	}
	function xu(e, t) {
		X = null, O.H = ks, t === ha || t === _a ? (t = Ca(), Il = 3) : t === ga ? (t = Ca(), Il = 4) : Il = t === qs ? 8 : typeof t == "object" && t && typeof t.then == "function" ? 6 : 1, Ll = t, Pl === null && (Hl = 1, Vs(e, hi(t, e.current)));
	}
	function $() {
		var e = Ja.current;
		return e === null ? !0 : (Fl & 4194048) === Fl ? Ya === null : (Fl & 62914560) === Fl || Fl & 536870912 ? e === Ya : !1;
	}
	function Su() {
		var e = O.H;
		return O.H = ks, e === null ? ks : e;
	}
	function Cu() {
		var e = O.A;
		return O.A = Al, e;
	}
	function wu() {
		Hl = 4, Rl || (Fl & 4194048) !== Fl && Ja.current !== null || (zl = !0), !(Ul & 134217727) && !(Wl & 134217727) || Nl === null || _u(Nl, Fl, Kl, !1);
	}
	function Tu(e, t, n) {
		var r = Ml;
		Ml |= 2;
		var i = Su(), a = Cu();
		(Nl !== e || Fl !== t) && (eu = null, bu(e, t)), t = !1;
		var o = Hl;
		a: do
			try {
				if (Il !== 0 && Pl !== null) {
					var s = Pl, c = Ll;
					switch (Il) {
						case 8:
							yu(), o = 6;
							break a;
						case 3:
						case 2:
						case 9:
						case 6:
							Ja.current === null && (t = !0);
							var l = Il;
							if (Il = 0, Ll = null, ju(e, s, c, l), n && zl) {
								o = 0;
								break a;
							}
							break;
						default: l = Il, Il = 0, Ll = null, ju(e, s, c, l);
					}
				}
				Eu(), o = Hl;
				break;
			} catch (t) {
				xu(e, t);
			}
		while (1);
		return t && e.shellSuspendCounter++, Vi = Bi = null, Ml = r, O.H = i, O.A = a, Pl === null && (Nl = null, Fl = 0, ri()), o;
	}
	function Eu() {
		for (; Pl !== null;) ku(Pl);
	}
	function Du(e, t) {
		var n = Ml;
		Ml |= 2;
		var r = Su(), a = Cu();
		Nl !== e || Fl !== t ? (eu = null, $l = Pe() + 500, bu(e, t)) : zl = et(e, t);
		a: do
			try {
				if (Il !== 0 && Pl !== null) {
					t = Pl;
					var o = Ll;
					b: switch (Il) {
						case 1:
							Il = 0, Ll = null, ju(e, t, o, 1);
							break;
						case 2:
						case 9:
							if (ya(o)) {
								Il = 0, Ll = null, Au(t);
								break;
							}
							t = function() {
								Il !== 2 && Il !== 9 || Nl !== e || (Il = 7), td(e);
							}, o.then(t, t);
							break a;
						case 3:
							Il = 7;
							break a;
						case 4:
							Il = 5;
							break a;
						case 7:
							ya(o) ? (Il = 0, Ll = null, Au(t)) : (Il = 0, Ll = null, ju(e, t, o, 7));
							break;
						case 5:
							var s = null;
							switch (Pl.tag) {
								case 26: s = Pl.memoizedState;
								case 5:
								case 27:
									var c = Pl;
									if (s ? Wf(s) : c.stateNode.complete) {
										Il = 0, Ll = null;
										var l = c.sibling;
										if (l !== null) Pl = l;
										else {
											var u = c.return;
											u === null ? Pl = null : (Pl = u, Mu(u));
										}
										break b;
									}
							}
							Il = 0, Ll = null, ju(e, t, o, 5);
							break;
						case 6:
							Il = 0, Ll = null, ju(e, t, o, 6);
							break;
						case 8:
							yu(), Hl = 6;
							break a;
						default: throw Error(i(462));
					}
				}
				Ou();
				break;
			} catch (t) {
				xu(e, t);
			}
		while (1);
		return Vi = Bi = null, O.H = r, O.A = a, Ml = n, Pl === null ? (Nl = null, Fl = 0, ri(), Hl) : 0;
	}
	function Ou() {
		for (; Pl !== null && !Me();) ku(Pl);
	}
	function ku(e) {
		var t = Cc(e.alternate, e, Vl);
		e.memoizedProps = e.pendingProps, t === null ? Mu(e) : Pl = t;
	}
	function Au(e) {
		var t = e, n = t.alternate;
		switch (t.tag) {
			case 15:
			case 0:
				t = sc(n, t, t.pendingProps, t.type, void 0, Fl);
				break;
			case 11:
				t = sc(n, t, t.pendingProps, t.type.render, t.ref, Fl);
				break;
			case 5: bo(t);
			default: Mc(n, t), t = Pl = I(t, Vl), t = Cc(n, t, Vl);
		}
		e.memoizedProps = e.pendingProps, t === null ? Mu(e) : Pl = t;
	}
	function ju(e, t, n, r) {
		Vi = Bi = null, bo(t), Ta = null, Ea = 0;
		var i = t.return;
		try {
			if (Ks(e, i, t, n, Fl)) {
				Hl = 1, Vs(e, hi(n, e.current)), Pl = null;
				return;
			}
		} catch (t) {
			if (i !== null) throw Pl = i, t;
			Hl = 1, Vs(e, hi(n, e.current)), Pl = null;
			return;
		}
		t.flags & 32768 ? (ki || r === 1 ? e = !0 : zl || Fl & 536870912 ? e = !1 : (Rl = e = !0, (r === 2 || r === 9 || r === 3 || r === 6) && (r = Ja.current, r !== null && r.tag === 13 && (r.flags |= 16384))), Nu(t, e)) : Mu(t);
	}
	function Mu(e) {
		var t = e;
		do {
			if (t.flags & 32768) {
				Nu(t, Rl);
				return;
			}
			e = t.return;
			var n = Ac(t.alternate, t, Vl);
			if (n !== null) {
				Pl = n;
				return;
			}
			if (t = t.sibling, t !== null) {
				Pl = t;
				return;
			}
			Pl = t = e;
		} while (t !== null);
		Hl === 0 && (Hl = 5);
	}
	function Nu(e, t) {
		do {
			var n = jc(e.alternate, e);
			if (n !== null) {
				n.flags &= 32767, Pl = n;
				return;
			}
			if (n = e.return, n !== null && (n.flags |= 32768, n.subtreeFlags = 0, n.deletions = null), !t && (e = e.sibling, e !== null)) {
				Pl = e;
				return;
			}
			Pl = e = n;
		} while (e !== null);
		Hl = 6, Pl = null;
	}
	function Pu(e, t, n, r, a, o, s, c, l) {
		e.cancelPendingCommit = null;
		do
			zu();
		while (nu !== 0);
		if (Ml & 6) throw Error(i(327));
		if (t !== null) {
			if (t === e.current) throw Error(i(177));
			if (o = t.lanes | t.childLanes, o |= ni, at(e, n, o, s, c, l), e === Nl && (Pl = Nl = null, Fl = 0), iu = t, ru = e, au = n, ou = o, su = a, cu = r, t.subtreeFlags & 10256 || t.flags & 10256 ? (e.callbackNode = null, e.callbackPriority = 0, Ju(Re, function() {
				return Bu(), null;
			})) : (e.callbackNode = null, e.callbackPriority = 0), r = (t.flags & 13878) != 0, t.subtreeFlags & 13878 || r) {
				r = O.T, O.T = null, a = k.p, k.p = 2, s = Ml, Ml |= 4;
				try {
					Zc(e, t, n);
				} finally {
					Ml = s, k.p = a, O.T = r;
				}
			}
			nu = 1, Fu(), Iu(), Lu();
		}
	}
	function Fu() {
		if (nu === 1) {
			nu = 0;
			var e = ru, t = iu, n = (t.flags & 13878) != 0;
			if (t.subtreeFlags & 13878 || n) {
				n = O.T, O.T = null;
				var r = k.p;
				k.p = 2;
				var i = Ml;
				Ml |= 4;
				try {
					ul(t, e);
					var a = zd, o = jr(e.containerInfo), s = a.focusedElem, c = a.selectionRange;
					if (o !== s && s && s.ownerDocument && Ar(s.ownerDocument.documentElement, s)) {
						if (c !== null && Mr(s)) {
							var l = c.start, u = c.end;
							if (u === void 0 && (u = l), "selectionStart" in s) s.selectionStart = l, s.selectionEnd = Math.min(u, s.value.length);
							else {
								var d = s.ownerDocument || document, f = d && d.defaultView || window;
								if (f.getSelection) {
									var p = f.getSelection(), m = s.textContent.length, h = Math.min(c.start, m), g = c.end === void 0 ? h : Math.min(c.end, m);
									!p.extend && h > g && (o = g, g = h, h = o);
									var _ = kr(s, h), v = kr(s, g);
									if (_ && v && (p.rangeCount !== 1 || p.anchorNode !== _.node || p.anchorOffset !== _.offset || p.focusNode !== v.node || p.focusOffset !== v.offset)) {
										var y = d.createRange();
										y.setStart(_.node, _.offset), p.removeAllRanges(), h > g ? (p.addRange(y), p.extend(v.node, v.offset)) : (y.setEnd(v.node, v.offset), p.addRange(y));
									}
								}
							}
						}
						for (d = [], p = s; p = p.parentNode;) p.nodeType === 1 && d.push({
							element: p,
							left: p.scrollLeft,
							top: p.scrollTop
						});
						for (typeof s.focus == "function" && s.focus(), s = 0; s < d.length; s++) {
							var b = d[s];
							b.element.scrollLeft = b.left, b.element.scrollTop = b.top;
						}
					}
					sp = !!Rd, zd = Rd = null;
				} finally {
					Ml = i, k.p = r, O.T = n;
				}
			}
			e.current = t, nu = 2;
		}
	}
	function Iu() {
		if (nu === 2) {
			nu = 0;
			var e = ru, t = iu, n = (t.flags & 8772) != 0;
			if (t.subtreeFlags & 8772 || n) {
				n = O.T, O.T = null;
				var r = k.p;
				k.p = 2;
				var i = Ml;
				Ml |= 4;
				try {
					Qc(e, t.alternate, t);
				} finally {
					Ml = i, k.p = r, O.T = n;
				}
			}
			nu = 3;
		}
	}
	function Lu() {
		if (nu === 4 || nu === 3) {
			nu = 0, Ne();
			var e = ru, t = iu, n = au, r = cu;
			t.subtreeFlags & 10256 || t.flags & 10256 ? nu = 5 : (nu = 0, iu = ru = null, Ru(e, e.pendingLanes));
			var i = e.pendingLanes;
			if (i === 0 && (tu = null), ut(n), t = t.stateNode, Ue && typeof Ue.onCommitFiberRoot == "function") try {
				Ue.onCommitFiberRoot(He, t, void 0, (t.current.flags & 128) == 128);
			} catch {}
			if (r !== null) {
				t = O.T, i = k.p, k.p = 2, O.T = null;
				try {
					for (var a = e.onRecoverableError, o = 0; o < r.length; o++) {
						var s = r[o];
						a(s.value, { componentStack: s.stack });
					}
				} finally {
					O.T = t, k.p = i;
				}
			}
			au & 3 && zu(), td(e), i = e.pendingLanes, n & 261930 && i & 42 ? e === uu ? lu++ : (lu = 0, uu = e) : lu = 0, nd(0, !1);
		}
	}
	function Ru(e, t) {
		(e.pooledCacheLanes &= t) === 0 && (t = e.pooledCache, t != null && (e.pooledCache = null, ra(t)));
	}
	function zu() {
		return Fu(), Iu(), Lu(), Bu();
	}
	function Bu() {
		if (nu !== 5) return !1;
		var e = ru, t = ou;
		ou = 0;
		var n = ut(au), r = O.T, a = k.p;
		try {
			k.p = 32 > n ? 32 : n, O.T = null, n = su, su = null;
			var o = ru, s = au;
			if (nu = 0, iu = ru = null, au = 0, Ml & 6) throw Error(i(331));
			var c = Ml;
			if (Ml |= 4, Dl(o.current), yl(o, o.current, s, n), Ml = c, nd(0, !1), Ue && typeof Ue.onPostCommitFiberRoot == "function") try {
				Ue.onPostCommitFiberRoot(He, o);
			} catch {}
			return !0;
		} finally {
			k.p = a, O.T = r, Ru(e, t);
		}
	}
	function Vu(e, t, n) {
		t = hi(n, t), t = Us(e.stateNode, t, 2), e = Ia(e, t, 2), e !== null && (it(e, 2), td(e));
	}
	function Hu(e, t, n) {
		if (e.tag === 3) Vu(e, e, n);
		else for (; t !== null;) {
			if (t.tag === 3) {
				Vu(t, e, n);
				break;
			} else if (t.tag === 1) {
				var r = t.stateNode;
				if (typeof t.type.getDerivedStateFromError == "function" || typeof r.componentDidCatch == "function" && (tu === null || !tu.has(r))) {
					e = hi(n, e), n = Ws(2), r = Ia(t, n, 2), r !== null && (Gs(n, r, t, e), it(r, 2), td(r));
					break;
				}
			}
			t = t.return;
		}
	}
	function Uu(e, t, n) {
		var r = e.pingCache;
		if (r === null) {
			r = e.pingCache = new jl();
			var i = /* @__PURE__ */ new Set();
			r.set(t, i);
		} else i = r.get(t), i === void 0 && (i = /* @__PURE__ */ new Set(), r.set(t, i));
		i.has(n) || (Bl = !0, i.add(n), e = Wu.bind(null, e, t, n), t.then(e, e));
	}
	function Wu(e, t, n) {
		var r = e.pingCache;
		r !== null && r.delete(t), e.pingedLanes |= e.suspendedLanes & n, e.warmLanes &= ~n, Nl === e && (Fl & n) === n && (Hl === 4 || Hl === 3 && (Fl & 62914560) === Fl && 300 > Pe() - Zl ? !(Ml & 2) && bu(e, 0) : Gl |= n, ql === Fl && (ql = 0)), td(e);
	}
	function Gu(e, t) {
		t === 0 && (t = nt()), e = oi(e, t), e !== null && (it(e, t), td(e));
	}
	function Ku(e) {
		var t = e.memoizedState, n = 0;
		t !== null && (n = t.retryLane), Gu(e, n);
	}
	function qu(e, t) {
		var n = 0;
		switch (e.tag) {
			case 31:
			case 13:
				var r = e.stateNode, a = e.memoizedState;
				a !== null && (n = a.retryLane);
				break;
			case 19:
				r = e.stateNode;
				break;
			case 22:
				r = e.stateNode._retryCache;
				break;
			default: throw Error(i(314));
		}
		r !== null && r.delete(t), Gu(e, n);
	}
	function Ju(e, t) {
		return Ae(e, t);
	}
	var Yu = null, Xu = null, Zu = !1, Qu = !1, $u = !1, ed = 0;
	function td(e) {
		e !== Xu && e.next === null && (Xu === null ? Yu = Xu = e : Xu = Xu.next = e), Qu = !0, Zu || (Zu = !0, cd());
	}
	function nd(e, t) {
		if (!$u && Qu) {
			$u = !0;
			do
				for (var n = !1, r = Yu; r !== null;) {
					if (!t) if (e !== 0) {
						var i = r.pendingLanes;
						if (i === 0) var a = 0;
						else {
							var o = r.suspendedLanes, s = r.pingedLanes;
							a = (1 << 31 - Ge(42 | e) + 1) - 1, a &= i & ~(o & ~s), a = a & 201326741 ? a & 201326741 | 1 : a ? a | 2 : 0;
						}
						a !== 0 && (n = !0, sd(r, a));
					} else a = Fl, a = $e(r, r === Nl ? a : 0, r.cancelPendingCommit !== null || r.timeoutHandle !== -1), !(a & 3) || et(r, a) || (n = !0, sd(r, a));
					r = r.next;
				}
			while (n);
			$u = !1;
		}
	}
	function rd() {
		id();
	}
	function id() {
		Qu = Zu = !1;
		var e = 0;
		ed !== 0 && Gd() && (e = ed);
		for (var t = Pe(), n = null, r = Yu; r !== null;) {
			var i = r.next, a = ad(r, t);
			a === 0 ? (r.next = null, n === null ? Yu = i : n.next = i, i === null && (Xu = n)) : (n = r, (e !== 0 || a & 3) && (Qu = !0)), r = i;
		}
		nu !== 0 && nu !== 5 || nd(e, !1), ed !== 0 && (ed = 0);
	}
	function ad(e, t) {
		for (var n = e.suspendedLanes, r = e.pingedLanes, i = e.expirationTimes, a = e.pendingLanes & -62914561; 0 < a;) {
			var o = 31 - Ge(a), s = 1 << o, c = i[o];
			c === -1 ? ((s & n) === 0 || (s & r) !== 0) && (i[o] = tt(s, t)) : c <= t && (e.expiredLanes |= s), a &= ~s;
		}
		if (t = Nl, n = Fl, n = $e(e, e === t ? n : 0, e.cancelPendingCommit !== null || e.timeoutHandle !== -1), r = e.callbackNode, n === 0 || e === t && (Il === 2 || Il === 9) || e.cancelPendingCommit !== null) return r !== null && r !== null && je(r), e.callbackNode = null, e.callbackPriority = 0;
		if (!(n & 3) || et(e, n)) {
			if (t = n & -n, t === e.callbackPriority) return t;
			switch (r !== null && je(r), ut(n)) {
				case 2:
				case 8:
					n = Le;
					break;
				case 32:
					n = Re;
					break;
				case 268435456:
					n = A;
					break;
				default: n = Re;
			}
			return r = od.bind(null, e), n = Ae(n, r), e.callbackPriority = t, e.callbackNode = n, t;
		}
		return r !== null && r !== null && je(r), e.callbackPriority = 2, e.callbackNode = null, 2;
	}
	function od(e, t) {
		if (nu !== 0 && nu !== 5) return e.callbackNode = null, e.callbackPriority = 0, null;
		var n = e.callbackNode;
		if (zu() && e.callbackNode !== n) return null;
		var r = Fl;
		return r = $e(e, e === Nl ? r : 0, e.cancelPendingCommit !== null || e.timeoutHandle !== -1), r === 0 ? null : (mu(e, r, t), ad(e, Pe()), e.callbackNode != null && e.callbackNode === n ? od.bind(null, e) : null);
	}
	function sd(e, t) {
		if (zu()) return null;
		mu(e, t, !0);
	}
	function cd() {
		Yd(function() {
			Ml & 6 ? Ae(Ie, rd) : id();
		});
	}
	function ld() {
		if (ed === 0) {
			var e = oa;
			e === 0 && (e = Ye, Ye <<= 1, !(Ye & 261888) && (Ye = 256)), ed = e;
		}
		return ed;
	}
	function ud(e) {
		return e == null || typeof e == "symbol" || typeof e == "boolean" ? null : typeof e == "function" ? e : an("" + e);
	}
	function dd(e, t) {
		var n = t.ownerDocument.createElement("input");
		return n.name = t.name, n.value = t.value, e.id && n.setAttribute("form", e.id), t.parentNode.insertBefore(n, t), e = new FormData(e), n.parentNode.removeChild(n), e;
	}
	function fd(e, t, n, r, i) {
		if (t === "submit" && n && n.stateNode === i) {
			var a = ud((i[ht] || null).action), o = r.submitter;
			o && (t = (t = o[ht] || null) ? ud(t.formAction) : o.getAttribute("formAction"), t !== null && (a = t, o = null));
			var s = new Dn("action", "action", null, r, i);
			e.push({
				event: s,
				listeners: [{
					instance: null,
					listener: function() {
						if (r.defaultPrevented) {
							if (ed !== 0) {
								var e = o ? dd(i, o) : new FormData(i);
								gs(n, {
									pending: !0,
									data: e,
									method: i.method,
									action: a
								}, null, e);
							}
						} else typeof a == "function" && (s.preventDefault(), e = o ? dd(i, o) : new FormData(i), gs(n, {
							pending: !0,
							data: e,
							method: i.method,
							action: a
						}, a, e));
					},
					currentTarget: i
				}]
			});
		}
	}
	for (var pd = 0; pd < Zr.length; pd++) {
		var md = Zr[pd];
		Qr(md.toLowerCase(), "on" + (md[0].toUpperCase() + md.slice(1)));
	}
	Qr(Ur, "onAnimationEnd"), Qr(Wr, "onAnimationIteration"), Qr(Gr, "onAnimationStart"), Qr("dblclick", "onDoubleClick"), Qr("focusin", "onFocus"), Qr("focusout", "onBlur"), Qr(Kr, "onTransitionRun"), Qr(qr, "onTransitionStart"), Qr(Jr, "onTransitionCancel"), Qr(Yr, "onTransitionEnd"), jt("onMouseEnter", ["mouseout", "mouseover"]), jt("onMouseLeave", ["mouseout", "mouseover"]), jt("onPointerEnter", ["pointerout", "pointerover"]), jt("onPointerLeave", ["pointerout", "pointerover"]), At("onChange", "change click focusin focusout input keydown keyup selectionchange".split(" ")), At("onSelect", "focusout contextmenu dragend focusin keydown keyup mousedown mouseup selectionchange".split(" ")), At("onBeforeInput", [
		"compositionend",
		"keypress",
		"textInput",
		"paste"
	]), At("onCompositionEnd", "compositionend focusout keydown keypress keyup mousedown".split(" ")), At("onCompositionStart", "compositionstart focusout keydown keypress keyup mousedown".split(" ")), At("onCompositionUpdate", "compositionupdate focusout keydown keypress keyup mousedown".split(" "));
	var hd = "abort canplay canplaythrough durationchange emptied encrypted ended error loadeddata loadedmetadata loadstart pause play playing progress ratechange resize seeked seeking stalled suspend timeupdate volumechange waiting".split(" "), gd = new Set("beforetoggle cancel close invalid load scroll scrollend toggle".split(" ").concat(hd));
	function _d(e, t) {
		t = (t & 4) != 0;
		for (var n = 0; n < e.length; n++) {
			var r = e[n], i = r.event;
			r = r.listeners;
			a: {
				var a = void 0;
				if (t) for (var o = r.length - 1; 0 <= o; o--) {
					var s = r[o], c = s.instance, l = s.currentTarget;
					if (s = s.listener, c !== a && i.isPropagationStopped()) break a;
					a = s, i.currentTarget = l;
					try {
						a(i);
					} catch (e) {
						$r(e);
					}
					i.currentTarget = null, a = c;
				}
				else for (o = 0; o < r.length; o++) {
					if (s = r[o], c = s.instance, l = s.currentTarget, s = s.listener, c !== a && i.isPropagationStopped()) break a;
					a = s, i.currentTarget = l;
					try {
						a(i);
					} catch (e) {
						$r(e);
					}
					i.currentTarget = null, a = c;
				}
			}
		}
	}
	function vd(e, t) {
		var n = t[_t];
		n === void 0 && (n = t[_t] = /* @__PURE__ */ new Set());
		var r = e + "__bubble";
		n.has(r) || (Sd(t, e, 2, !1), n.add(r));
	}
	function yd(e, t, n) {
		var r = 0;
		t && (r |= 4), Sd(n, e, r, t);
	}
	var bd = "_reactListening" + Math.random().toString(36).slice(2);
	function xd(e) {
		if (!e[bd]) {
			e[bd] = !0, Ot.forEach(function(t) {
				t !== "selectionchange" && (gd.has(t) || yd(t, !1, e), yd(t, !0, e));
			});
			var t = e.nodeType === 9 ? e : e.ownerDocument;
			t === null || t[bd] || (t[bd] = !0, yd("selectionchange", !1, t));
		}
	}
	function Sd(e, t, n, r) {
		switch (mp(t)) {
			case 2:
				var i = cp;
				break;
			case 8:
				i = lp;
				break;
			default: i = up;
		}
		n = i.bind(null, t, n, e), i = void 0, !gn || t !== "touchstart" && t !== "touchmove" && t !== "wheel" || (i = !0), r ? i === void 0 ? e.addEventListener(t, n, !0) : e.addEventListener(t, n, {
			capture: !0,
			passive: i
		}) : i === void 0 ? e.addEventListener(t, n, !1) : e.addEventListener(t, n, { passive: i });
	}
	function Cd(e, t, n, r, i) {
		var a = r;
		if (!(t & 1) && !(t & 2) && r !== null) a: for (;;) {
			if (r === null) return;
			var s = r.tag;
			if (s === 3 || s === 4) {
				var c = r.stateNode.containerInfo;
				if (c === i) break;
				if (s === 4) for (s = r.return; s !== null;) {
					var l = s.tag;
					if ((l === 3 || l === 4) && s.stateNode.containerInfo === i) return;
					s = s.return;
				}
				for (; c !== null;) {
					if (s = Ct(c), s === null) return;
					if (l = s.tag, l === 5 || l === 6 || l === 26 || l === 27) {
						r = a = s;
						continue a;
					}
					c = c.parentNode;
				}
			}
			r = r.return;
		}
		pn(function() {
			var r = a, i = cn(n), s = [];
			a: {
				var c = Xr.get(e);
				if (c !== void 0) {
					var l = Dn, u = e;
					switch (e) {
						case "keypress": if (Sn(n) === 0) break a;
						case "keydown":
						case "keyup":
							l = Wn;
							break;
						case "focusin":
							u = "focus", l = In;
							break;
						case "focusout":
							u = "blur", l = In;
							break;
						case "beforeblur":
						case "afterblur":
							l = In;
							break;
						case "click": if (n.button === 2) break a;
						case "auxclick":
						case "dblclick":
						case "mousedown":
						case "mousemove":
						case "mouseup":
						case "mouseout":
						case "mouseover":
						case "contextmenu":
							l = Pn;
							break;
						case "drag":
						case "dragend":
						case "dragenter":
						case "dragexit":
						case "dragleave":
						case "dragover":
						case "dragstart":
						case "drop":
							l = Fn;
							break;
						case "touchcancel":
						case "touchend":
						case "touchmove":
						case "touchstart":
							l = Kn;
							break;
						case Ur:
						case Wr:
						case Gr:
							l = Ln;
							break;
						case Yr:
							l = qn;
							break;
						case "scroll":
						case "scrollend":
							l = kn;
							break;
						case "wheel":
							l = Jn;
							break;
						case "copy":
						case "cut":
						case "paste":
							l = Rn;
							break;
						case "gotpointercapture":
						case "lostpointercapture":
						case "pointercancel":
						case "pointerdown":
						case "pointermove":
						case "pointerout":
						case "pointerover":
						case "pointerup":
							l = Gn;
							break;
						case "toggle":
						case "beforetoggle": l = Yn;
					}
					var d = (t & 4) != 0, f = !d && (e === "scroll" || e === "scrollend"), p = d ? c === null ? null : c + "Capture" : c;
					d = [];
					for (var m = r, h; m !== null;) {
						var g = m;
						if (h = g.stateNode, g = g.tag, g !== 5 && g !== 26 && g !== 27 || h === null || p === null || (g = mn(m, p), g != null && d.push(wd(m, g, h))), f) break;
						m = m.return;
					}
					0 < d.length && (c = new l(c, u, null, n, i), s.push({
						event: c,
						listeners: d
					}));
				}
			}
			if (!(t & 7)) {
				a: {
					if (c = e === "mouseover" || e === "pointerover", l = e === "mouseout" || e === "pointerout", c && n !== sn && (u = n.relatedTarget || n.fromElement) && (Ct(u) || u[gt])) break a;
					if ((l || c) && (c = i.window === i ? i : (c = i.ownerDocument) ? c.defaultView || c.parentWindow : window, l ? (u = n.relatedTarget || n.toElement, l = r, u = u ? Ct(u) : null, u !== null && (f = o(u), d = u.tag, u !== f || d !== 5 && d !== 27 && d !== 6) && (u = null)) : (l = null, u = r), l !== u)) {
						if (d = Pn, g = "onMouseLeave", p = "onMouseEnter", m = "mouse", (e === "pointerout" || e === "pointerover") && (d = Gn, g = "onPointerLeave", p = "onPointerEnter", m = "pointer"), f = l == null ? c : Tt(l), h = u == null ? c : Tt(u), c = new d(g, m + "leave", l, n, i), c.target = f, c.relatedTarget = h, g = null, Ct(i) === r && (d = new d(p, m + "enter", u, n, i), d.target = h, d.relatedTarget = f, g = d), f = g, l && u) b: {
							for (d = Ed, p = l, m = u, h = 0, g = p; g; g = d(g)) h++;
							g = 0;
							for (var _ = m; _; _ = d(_)) g++;
							for (; 0 < h - g;) p = d(p), h--;
							for (; 0 < g - h;) m = d(m), g--;
							for (; h--;) {
								if (p === m || m !== null && p === m.alternate) {
									d = p;
									break b;
								}
								p = d(p), m = d(m);
							}
							d = null;
						}
						else d = null;
						l !== null && Dd(s, c, l, d, !1), u !== null && f !== null && Dd(s, f, u, d, !0);
					}
				}
				a: {
					if (c = r ? Tt(r) : window, l = c.nodeName && c.nodeName.toLowerCase(), l === "select" || l === "input" && c.type === "file") var v = hr;
					else if (lr(c)) if (gr) v = wr;
					else {
						v = Sr;
						var y = xr;
					}
					else l = c.nodeName, !l || l.toLowerCase() !== "input" || c.type !== "checkbox" && c.type !== "radio" ? r && tn(r.elementType) && (v = hr) : v = Cr;
					if (v &&= v(e, r)) {
						ur(s, v, n, i);
						break a;
					}
					y && y(e, c, r), e === "focusout" && r && c.type === "number" && r.memoizedProps.value != null && qt(c, "number", c.value);
				}
				switch (y = r ? Tt(r) : window, e) {
					case "focusin":
						(lr(y) || y.contentEditable === "true") && (Pr = y, Fr = r, Ir = null);
						break;
					case "focusout":
						Ir = Fr = Pr = null;
						break;
					case "mousedown":
						Lr = !0;
						break;
					case "contextmenu":
					case "mouseup":
					case "dragend":
						Lr = !1, Rr(s, n, i);
						break;
					case "selectionchange": if (Nr) break;
					case "keydown":
					case "keyup": Rr(s, n, i);
				}
				var b;
				if (Zn) b: {
					switch (e) {
						case "compositionstart":
							var x = "onCompositionStart";
							break b;
						case "compositionend":
							x = "onCompositionEnd";
							break b;
						case "compositionupdate":
							x = "onCompositionUpdate";
							break b;
					}
					x = void 0;
				}
				else ar ? rr(e, n) && (x = "onCompositionEnd") : e === "keydown" && n.keyCode === 229 && (x = "onCompositionStart");
				x && (er && n.locale !== "ko" && (ar || x !== "onCompositionStart" ? x === "onCompositionEnd" && ar && (b = xn()) : (vn = i, yn = "value" in vn ? vn.value : vn.textContent, ar = !0)), y = Td(r, x), 0 < y.length && (x = new zn(x, e, null, n, i), s.push({
					event: x,
					listeners: y
				}), b ? x.data = b : (b = ir(n), b !== null && (x.data = b)))), (b = $n ? or(e, n) : sr(e, n)) && (x = Td(r, "onBeforeInput"), 0 < x.length && (y = new zn("onBeforeInput", "beforeinput", null, n, i), s.push({
					event: y,
					listeners: x
				}), y.data = b)), fd(s, e, r, n, i);
			}
			_d(s, t);
		});
	}
	function wd(e, t, n) {
		return {
			instance: e,
			listener: t,
			currentTarget: n
		};
	}
	function Td(e, t) {
		for (var n = t + "Capture", r = []; e !== null;) {
			var i = e, a = i.stateNode;
			if (i = i.tag, i !== 5 && i !== 26 && i !== 27 || a === null || (i = mn(e, n), i != null && r.unshift(wd(e, i, a)), i = mn(e, t), i != null && r.push(wd(e, i, a))), e.tag === 3) return r;
			e = e.return;
		}
		return [];
	}
	function Ed(e) {
		if (e === null) return null;
		do
			e = e.return;
		while (e && e.tag !== 5 && e.tag !== 27);
		return e || null;
	}
	function Dd(e, t, n, r, i) {
		for (var a = t._reactName, o = []; n !== null && n !== r;) {
			var s = n, c = s.alternate, l = s.stateNode;
			if (s = s.tag, c !== null && c === r) break;
			s !== 5 && s !== 26 && s !== 27 || l === null || (c = l, i ? (l = mn(n, a), l != null && o.unshift(wd(n, l, c))) : i || (l = mn(n, a), l != null && o.push(wd(n, l, c)))), n = n.return;
		}
		o.length !== 0 && e.push({
			event: t,
			listeners: o
		});
	}
	var Od = /\r\n?/g, kd = /\u0000|\uFFFD/g;
	function Ad(e) {
		return (typeof e == "string" ? e : "" + e).replace(Od, "\n").replace(kd, "");
	}
	function jd(e, t) {
		return t = Ad(t), Ad(e) === t;
	}
	function Md(e, t, n, r, a, o) {
		switch (n) {
			case "children":
				typeof r == "string" ? t === "body" || t === "textarea" && r === "" || Zt(e, r) : (typeof r == "number" || typeof r == "bigint") && t !== "body" && Zt(e, "" + r);
				break;
			case "className":
				Lt(e, "class", r);
				break;
			case "tabIndex":
				Lt(e, "tabindex", r);
				break;
			case "dir":
			case "role":
			case "viewBox":
			case "width":
			case "height":
				Lt(e, n, r);
				break;
			case "style":
				en(e, r, o);
				break;
			case "data": if (t !== "object") {
				Lt(e, "data", r);
				break;
			}
			case "src":
			case "href":
				if (r === "" && (t !== "a" || n !== "href")) {
					e.removeAttribute(n);
					break;
				}
				if (r == null || typeof r == "function" || typeof r == "symbol" || typeof r == "boolean") {
					e.removeAttribute(n);
					break;
				}
				r = an("" + r), e.setAttribute(n, r);
				break;
			case "action":
			case "formAction":
				if (typeof r == "function") {
					e.setAttribute(n, "javascript:throw new Error('A React form was unexpectedly submitted. If you called form.submit() manually, consider using form.requestSubmit() instead. If you\\'re trying to use event.stopPropagation() in a submit event handler, consider also calling event.preventDefault().')");
					break;
				} else typeof o == "function" && (n === "formAction" ? (t !== "input" && Md(e, t, "name", a.name, a, null), Md(e, t, "formEncType", a.formEncType, a, null), Md(e, t, "formMethod", a.formMethod, a, null), Md(e, t, "formTarget", a.formTarget, a, null)) : (Md(e, t, "encType", a.encType, a, null), Md(e, t, "method", a.method, a, null), Md(e, t, "target", a.target, a, null)));
				if (r == null || typeof r == "symbol" || typeof r == "boolean") {
					e.removeAttribute(n);
					break;
				}
				r = an("" + r), e.setAttribute(n, r);
				break;
			case "onClick":
				r != null && (e.onclick = on);
				break;
			case "onScroll":
				r != null && vd("scroll", e);
				break;
			case "onScrollEnd":
				r != null && vd("scrollend", e);
				break;
			case "dangerouslySetInnerHTML":
				if (r != null) {
					if (typeof r != "object" || !("__html" in r)) throw Error(i(61));
					if (n = r.__html, n != null) {
						if (a.children != null) throw Error(i(60));
						e.innerHTML = n;
					}
				}
				break;
			case "multiple":
				e.multiple = r && typeof r != "function" && typeof r != "symbol";
				break;
			case "muted":
				e.muted = r && typeof r != "function" && typeof r != "symbol";
				break;
			case "suppressContentEditableWarning":
			case "suppressHydrationWarning":
			case "defaultValue":
			case "defaultChecked":
			case "innerHTML":
			case "ref": break;
			case "autoFocus": break;
			case "xlinkHref":
				if (r == null || typeof r == "function" || typeof r == "boolean" || typeof r == "symbol") {
					e.removeAttribute("xlink:href");
					break;
				}
				n = an("" + r), e.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", n);
				break;
			case "contentEditable":
			case "spellCheck":
			case "draggable":
			case "value":
			case "autoReverse":
			case "externalResourcesRequired":
			case "focusable":
			case "preserveAlpha":
				r != null && typeof r != "function" && typeof r != "symbol" ? e.setAttribute(n, "" + r) : e.removeAttribute(n);
				break;
			case "inert":
			case "allowFullScreen":
			case "async":
			case "autoPlay":
			case "controls":
			case "default":
			case "defer":
			case "disabled":
			case "disablePictureInPicture":
			case "disableRemotePlayback":
			case "formNoValidate":
			case "hidden":
			case "loop":
			case "noModule":
			case "noValidate":
			case "open":
			case "playsInline":
			case "readOnly":
			case "required":
			case "reversed":
			case "scoped":
			case "seamless":
			case "itemScope":
				r && typeof r != "function" && typeof r != "symbol" ? e.setAttribute(n, "") : e.removeAttribute(n);
				break;
			case "capture":
			case "download":
				!0 === r ? e.setAttribute(n, "") : !1 !== r && r != null && typeof r != "function" && typeof r != "symbol" ? e.setAttribute(n, r) : e.removeAttribute(n);
				break;
			case "cols":
			case "rows":
			case "size":
			case "span":
				r != null && typeof r != "function" && typeof r != "symbol" && !isNaN(r) && 1 <= r ? e.setAttribute(n, r) : e.removeAttribute(n);
				break;
			case "rowSpan":
			case "start":
				r == null || typeof r == "function" || typeof r == "symbol" || isNaN(r) ? e.removeAttribute(n) : e.setAttribute(n, r);
				break;
			case "popover":
				vd("beforetoggle", e), vd("toggle", e), It(e, "popover", r);
				break;
			case "xlinkActuate":
				Rt(e, "http://www.w3.org/1999/xlink", "xlink:actuate", r);
				break;
			case "xlinkArcrole":
				Rt(e, "http://www.w3.org/1999/xlink", "xlink:arcrole", r);
				break;
			case "xlinkRole":
				Rt(e, "http://www.w3.org/1999/xlink", "xlink:role", r);
				break;
			case "xlinkShow":
				Rt(e, "http://www.w3.org/1999/xlink", "xlink:show", r);
				break;
			case "xlinkTitle":
				Rt(e, "http://www.w3.org/1999/xlink", "xlink:title", r);
				break;
			case "xlinkType":
				Rt(e, "http://www.w3.org/1999/xlink", "xlink:type", r);
				break;
			case "xmlBase":
				Rt(e, "http://www.w3.org/XML/1998/namespace", "xml:base", r);
				break;
			case "xmlLang":
				Rt(e, "http://www.w3.org/XML/1998/namespace", "xml:lang", r);
				break;
			case "xmlSpace":
				Rt(e, "http://www.w3.org/XML/1998/namespace", "xml:space", r);
				break;
			case "is":
				It(e, "is", r);
				break;
			case "innerText":
			case "textContent": break;
			default: (!(2 < n.length) || n[0] !== "o" && n[0] !== "O" || n[1] !== "n" && n[1] !== "N") && (n = nn.get(n) || n, It(e, n, r));
		}
	}
	function Nd(e, t, n, r, a, o) {
		switch (n) {
			case "style":
				en(e, r, o);
				break;
			case "dangerouslySetInnerHTML":
				if (r != null) {
					if (typeof r != "object" || !("__html" in r)) throw Error(i(61));
					if (n = r.__html, n != null) {
						if (a.children != null) throw Error(i(60));
						e.innerHTML = n;
					}
				}
				break;
			case "children":
				typeof r == "string" ? Zt(e, r) : (typeof r == "number" || typeof r == "bigint") && Zt(e, "" + r);
				break;
			case "onScroll":
				r != null && vd("scroll", e);
				break;
			case "onScrollEnd":
				r != null && vd("scrollend", e);
				break;
			case "onClick":
				r != null && (e.onclick = on);
				break;
			case "suppressContentEditableWarning":
			case "suppressHydrationWarning":
			case "innerHTML":
			case "ref": break;
			case "innerText":
			case "textContent": break;
			default: if (!kt.hasOwnProperty(n)) a: {
				if (n[0] === "o" && n[1] === "n" && (a = n.endsWith("Capture"), t = n.slice(2, a ? n.length - 7 : void 0), o = e[ht] || null, o = o == null ? null : o[n], typeof o == "function" && e.removeEventListener(t, o, a), typeof r == "function")) {
					typeof o != "function" && o !== null && (n in e ? e[n] = null : e.hasAttribute(n) && e.removeAttribute(n)), e.addEventListener(t, r, a);
					break a;
				}
				n in e ? e[n] = r : !0 === r ? e.setAttribute(n, "") : It(e, n, r);
			}
		}
	}
	function Pd(e, t, n) {
		switch (t) {
			case "div":
			case "span":
			case "svg":
			case "path":
			case "a":
			case "g":
			case "p":
			case "li": break;
			case "img":
				vd("error", e), vd("load", e);
				var r = !1, a = !1, o;
				for (o in n) if (n.hasOwnProperty(o)) {
					var s = n[o];
					if (s != null) switch (o) {
						case "src":
							r = !0;
							break;
						case "srcSet":
							a = !0;
							break;
						case "children":
						case "dangerouslySetInnerHTML": throw Error(i(137, t));
						default: Md(e, t, o, s, n, null);
					}
				}
				a && Md(e, t, "srcSet", n.srcSet, n, null), r && Md(e, t, "src", n.src, n, null);
				return;
			case "input":
				vd("invalid", e);
				var c = o = s = a = null, l = null, u = null;
				for (r in n) if (n.hasOwnProperty(r)) {
					var d = n[r];
					if (d != null) switch (r) {
						case "name":
							a = d;
							break;
						case "type":
							s = d;
							break;
						case "checked":
							l = d;
							break;
						case "defaultChecked":
							u = d;
							break;
						case "value":
							o = d;
							break;
						case "defaultValue":
							c = d;
							break;
						case "children":
						case "dangerouslySetInnerHTML":
							if (d != null) throw Error(i(137, t));
							break;
						default: Md(e, t, r, d, n, null);
					}
				}
				Kt(e, o, c, l, u, s, a, !1);
				return;
			case "select":
				for (a in vd("invalid", e), r = s = o = null, n) if (n.hasOwnProperty(a) && (c = n[a], c != null)) switch (a) {
					case "value":
						o = c;
						break;
					case "defaultValue":
						s = c;
						break;
					case "multiple": r = c;
					default: Md(e, t, a, c, n, null);
				}
				t = o, n = s, e.multiple = !!r, t == null ? n != null && Jt(e, !!r, n, !0) : Jt(e, !!r, t, !1);
				return;
			case "textarea":
				for (s in vd("invalid", e), o = a = r = null, n) if (n.hasOwnProperty(s) && (c = n[s], c != null)) switch (s) {
					case "value":
						r = c;
						break;
					case "defaultValue":
						a = c;
						break;
					case "children":
						o = c;
						break;
					case "dangerouslySetInnerHTML":
						if (c != null) throw Error(i(91));
						break;
					default: Md(e, t, s, c, n, null);
				}
				Xt(e, r, a, o);
				return;
			case "option":
				for (l in n) if (n.hasOwnProperty(l) && (r = n[l], r != null)) switch (l) {
					case "selected":
						e.selected = r && typeof r != "function" && typeof r != "symbol";
						break;
					default: Md(e, t, l, r, n, null);
				}
				return;
			case "dialog":
				vd("beforetoggle", e), vd("toggle", e), vd("cancel", e), vd("close", e);
				break;
			case "iframe":
			case "object":
				vd("load", e);
				break;
			case "video":
			case "audio":
				for (r = 0; r < hd.length; r++) vd(hd[r], e);
				break;
			case "image":
				vd("error", e), vd("load", e);
				break;
			case "details":
				vd("toggle", e);
				break;
			case "embed":
			case "source":
			case "link": vd("error", e), vd("load", e);
			case "area":
			case "base":
			case "br":
			case "col":
			case "hr":
			case "keygen":
			case "meta":
			case "param":
			case "track":
			case "wbr":
			case "menuitem":
				for (u in n) if (n.hasOwnProperty(u) && (r = n[u], r != null)) switch (u) {
					case "children":
					case "dangerouslySetInnerHTML": throw Error(i(137, t));
					default: Md(e, t, u, r, n, null);
				}
				return;
			default: if (tn(t)) {
				for (d in n) n.hasOwnProperty(d) && (r = n[d], r !== void 0 && Nd(e, t, d, r, n, void 0));
				return;
			}
		}
		for (c in n) n.hasOwnProperty(c) && (r = n[c], r != null && Md(e, t, c, r, n, null));
	}
	function Fd(e, t, n, r) {
		switch (t) {
			case "div":
			case "span":
			case "svg":
			case "path":
			case "a":
			case "g":
			case "p":
			case "li": break;
			case "input":
				var a = null, o = null, s = null, c = null, l = null, u = null, d = null;
				for (m in n) {
					var f = n[m];
					if (n.hasOwnProperty(m) && f != null) switch (m) {
						case "checked": break;
						case "value": break;
						case "defaultValue": l = f;
						default: r.hasOwnProperty(m) || Md(e, t, m, null, r, f);
					}
				}
				for (var p in r) {
					var m = r[p];
					if (f = n[p], r.hasOwnProperty(p) && (m != null || f != null)) switch (p) {
						case "type":
							o = m;
							break;
						case "name":
							a = m;
							break;
						case "checked":
							u = m;
							break;
						case "defaultChecked":
							d = m;
							break;
						case "value":
							s = m;
							break;
						case "defaultValue":
							c = m;
							break;
						case "children":
						case "dangerouslySetInnerHTML":
							if (m != null) throw Error(i(137, t));
							break;
						default: m !== f && Md(e, t, p, m, r, f);
					}
				}
				Gt(e, s, c, l, u, d, o, a);
				return;
			case "select":
				for (o in m = s = c = p = null, n) if (l = n[o], n.hasOwnProperty(o) && l != null) switch (o) {
					case "value": break;
					case "multiple": m = l;
					default: r.hasOwnProperty(o) || Md(e, t, o, null, r, l);
				}
				for (a in r) if (o = r[a], l = n[a], r.hasOwnProperty(a) && (o != null || l != null)) switch (a) {
					case "value":
						p = o;
						break;
					case "defaultValue":
						c = o;
						break;
					case "multiple": s = o;
					default: o !== l && Md(e, t, a, o, r, l);
				}
				t = c, n = s, r = m, p == null ? !!r != !!n && (t == null ? Jt(e, !!n, n ? [] : "", !1) : Jt(e, !!n, t, !0)) : Jt(e, !!n, p, !1);
				return;
			case "textarea":
				for (c in m = p = null, n) if (a = n[c], n.hasOwnProperty(c) && a != null && !r.hasOwnProperty(c)) switch (c) {
					case "value": break;
					case "children": break;
					default: Md(e, t, c, null, r, a);
				}
				for (s in r) if (a = r[s], o = n[s], r.hasOwnProperty(s) && (a != null || o != null)) switch (s) {
					case "value":
						p = a;
						break;
					case "defaultValue":
						m = a;
						break;
					case "children": break;
					case "dangerouslySetInnerHTML":
						if (a != null) throw Error(i(91));
						break;
					default: a !== o && Md(e, t, s, a, r, o);
				}
				Yt(e, p, m);
				return;
			case "option":
				for (var h in n) if (p = n[h], n.hasOwnProperty(h) && p != null && !r.hasOwnProperty(h)) switch (h) {
					case "selected":
						e.selected = !1;
						break;
					default: Md(e, t, h, null, r, p);
				}
				for (l in r) if (p = r[l], m = n[l], r.hasOwnProperty(l) && p !== m && (p != null || m != null)) switch (l) {
					case "selected":
						e.selected = p && typeof p != "function" && typeof p != "symbol";
						break;
					default: Md(e, t, l, p, r, m);
				}
				return;
			case "img":
			case "link":
			case "area":
			case "base":
			case "br":
			case "col":
			case "embed":
			case "hr":
			case "keygen":
			case "meta":
			case "param":
			case "source":
			case "track":
			case "wbr":
			case "menuitem":
				for (var g in n) p = n[g], n.hasOwnProperty(g) && p != null && !r.hasOwnProperty(g) && Md(e, t, g, null, r, p);
				for (u in r) if (p = r[u], m = n[u], r.hasOwnProperty(u) && p !== m && (p != null || m != null)) switch (u) {
					case "children":
					case "dangerouslySetInnerHTML":
						if (p != null) throw Error(i(137, t));
						break;
					default: Md(e, t, u, p, r, m);
				}
				return;
			default: if (tn(t)) {
				for (var _ in n) p = n[_], n.hasOwnProperty(_) && p !== void 0 && !r.hasOwnProperty(_) && Nd(e, t, _, void 0, r, p);
				for (d in r) p = r[d], m = n[d], !r.hasOwnProperty(d) || p === m || p === void 0 && m === void 0 || Nd(e, t, d, p, r, m);
				return;
			}
		}
		for (var v in n) p = n[v], n.hasOwnProperty(v) && p != null && !r.hasOwnProperty(v) && Md(e, t, v, null, r, p);
		for (f in r) p = r[f], m = n[f], !r.hasOwnProperty(f) || p === m || p == null && m == null || Md(e, t, f, p, r, m);
	}
	function Id(e) {
		switch (e) {
			case "css":
			case "script":
			case "font":
			case "img":
			case "image":
			case "input":
			case "link": return !0;
			default: return !1;
		}
	}
	function Ld() {
		if (typeof performance.getEntriesByType == "function") {
			for (var e = 0, t = 0, n = performance.getEntriesByType("resource"), r = 0; r < n.length; r++) {
				var i = n[r], a = i.transferSize, o = i.initiatorType, s = i.duration;
				if (a && s && Id(o)) {
					for (o = 0, s = i.responseEnd, r += 1; r < n.length; r++) {
						var c = n[r], l = c.startTime;
						if (l > s) break;
						var u = c.transferSize, d = c.initiatorType;
						u && Id(d) && (c = c.responseEnd, o += u * (c < s ? 1 : (s - l) / (c - l)));
					}
					if (--r, t += 8 * (a + o) / (i.duration / 1e3), e++, 10 < e) break;
				}
			}
			if (0 < e) return t / e / 1e6;
		}
		return navigator.connection && (e = navigator.connection.downlink, typeof e == "number") ? e : 5;
	}
	var Rd = null, zd = null;
	function Bd(e) {
		return e.nodeType === 9 ? e : e.ownerDocument;
	}
	function Vd(e) {
		switch (e) {
			case "http://www.w3.org/2000/svg": return 1;
			case "http://www.w3.org/1998/Math/MathML": return 2;
			default: return 0;
		}
	}
	function Hd(e, t) {
		if (e === 0) switch (t) {
			case "svg": return 1;
			case "math": return 2;
			default: return 0;
		}
		return e === 1 && t === "foreignObject" ? 0 : e;
	}
	function Ud(e, t) {
		return e === "textarea" || e === "noscript" || typeof t.children == "string" || typeof t.children == "number" || typeof t.children == "bigint" || typeof t.dangerouslySetInnerHTML == "object" && t.dangerouslySetInnerHTML !== null && t.dangerouslySetInnerHTML.__html != null;
	}
	var Wd = null;
	function Gd() {
		var e = window.event;
		return e && e.type === "popstate" ? e === Wd ? !1 : (Wd = e, !0) : (Wd = null, !1);
	}
	var Kd = typeof setTimeout == "function" ? setTimeout : void 0, qd = typeof clearTimeout == "function" ? clearTimeout : void 0, Jd = typeof Promise == "function" ? Promise : void 0, Yd = typeof queueMicrotask == "function" ? queueMicrotask : Jd === void 0 ? Kd : function(e) {
		return Jd.resolve(null).then(e).catch(Xd);
	};
	function Xd(e) {
		setTimeout(function() {
			throw e;
		});
	}
	function Zd(e) {
		return e === "head";
	}
	function Qd(e, t) {
		var n = t, r = 0;
		do {
			var i = n.nextSibling;
			if (e.removeChild(n), i && i.nodeType === 8) if (n = i.data, n === "/$" || n === "/&") {
				if (r === 0) {
					e.removeChild(i), Np(t);
					return;
				}
				r--;
			} else if (n === "$" || n === "$?" || n === "$~" || n === "$!" || n === "&") r++;
			else if (n === "html") pf(e.ownerDocument.documentElement);
			else if (n === "head") {
				n = e.ownerDocument.head, pf(n);
				for (var a = n.firstChild; a;) {
					var o = a.nextSibling, s = a.nodeName;
					a[xt] || s === "SCRIPT" || s === "STYLE" || s === "LINK" && a.rel.toLowerCase() === "stylesheet" || n.removeChild(a), a = o;
				}
			} else n === "body" && pf(e.ownerDocument.body);
			n = i;
		} while (n);
		Np(t);
	}
	function $d(e, t) {
		var n = e;
		e = 0;
		do {
			var r = n.nextSibling;
			if (n.nodeType === 1 ? t ? (n._stashedDisplay = n.style.display, n.style.display = "none") : (n.style.display = n._stashedDisplay || "", n.getAttribute("style") === "" && n.removeAttribute("style")) : n.nodeType === 3 && (t ? (n._stashedText = n.nodeValue, n.nodeValue = "") : n.nodeValue = n._stashedText || ""), r && r.nodeType === 8) if (n = r.data, n === "/$") {
				if (e === 0) break;
				e--;
			} else n !== "$" && n !== "$?" && n !== "$~" && n !== "$!" || e++;
			n = r;
		} while (n);
	}
	function ef(e) {
		var t = e.firstChild;
		for (t && t.nodeType === 10 && (t = t.nextSibling); t;) {
			var n = t;
			switch (t = t.nextSibling, n.nodeName) {
				case "HTML":
				case "HEAD":
				case "BODY":
					ef(n), St(n);
					continue;
				case "SCRIPT":
				case "STYLE": continue;
				case "LINK": if (n.rel.toLowerCase() === "stylesheet") continue;
			}
			e.removeChild(n);
		}
	}
	function tf(e, t, n, r) {
		for (; e.nodeType === 1;) {
			var i = n;
			if (e.nodeName.toLowerCase() !== t.toLowerCase()) {
				if (!r && (e.nodeName !== "INPUT" || e.type !== "hidden")) break;
			} else if (!r) if (t === "input" && e.type === "hidden") {
				var a = i.name == null ? null : "" + i.name;
				if (i.type === "hidden" && e.getAttribute("name") === a) return e;
			} else return e;
			else if (!e[xt]) switch (t) {
				case "meta":
					if (!e.hasAttribute("itemprop")) break;
					return e;
				case "link":
					if (a = e.getAttribute("rel"), a === "stylesheet" && e.hasAttribute("data-precedence") || a !== i.rel || e.getAttribute("href") !== (i.href == null || i.href === "" ? null : i.href) || e.getAttribute("crossorigin") !== (i.crossOrigin == null ? null : i.crossOrigin) || e.getAttribute("title") !== (i.title == null ? null : i.title)) break;
					return e;
				case "style":
					if (e.hasAttribute("data-precedence")) break;
					return e;
				case "script":
					if (a = e.getAttribute("src"), (a !== (i.src == null ? null : i.src) || e.getAttribute("type") !== (i.type == null ? null : i.type) || e.getAttribute("crossorigin") !== (i.crossOrigin == null ? null : i.crossOrigin)) && a && e.hasAttribute("async") && !e.hasAttribute("itemprop")) break;
					return e;
				default: return e;
			}
			if (e = cf(e.nextSibling), e === null) break;
		}
		return null;
	}
	function nf(e, t, n) {
		if (t === "") return null;
		for (; e.nodeType !== 3;) if ((e.nodeType !== 1 || e.nodeName !== "INPUT" || e.type !== "hidden") && !n || (e = cf(e.nextSibling), e === null)) return null;
		return e;
	}
	function rf(e, t) {
		for (; e.nodeType !== 8;) if ((e.nodeType !== 1 || e.nodeName !== "INPUT" || e.type !== "hidden") && !t || (e = cf(e.nextSibling), e === null)) return null;
		return e;
	}
	function af(e) {
		return e.data === "$?" || e.data === "$~";
	}
	function of(e) {
		return e.data === "$!" || e.data === "$?" && e.ownerDocument.readyState !== "loading";
	}
	function sf(e, t) {
		var n = e.ownerDocument;
		if (e.data === "$~") e._reactRetry = t;
		else if (e.data !== "$?" || n.readyState !== "loading") t();
		else {
			var r = function() {
				t(), n.removeEventListener("DOMContentLoaded", r);
			};
			n.addEventListener("DOMContentLoaded", r), e._reactRetry = r;
		}
	}
	function cf(e) {
		for (; e != null; e = e.nextSibling) {
			var t = e.nodeType;
			if (t === 1 || t === 3) break;
			if (t === 8) {
				if (t = e.data, t === "$" || t === "$!" || t === "$?" || t === "$~" || t === "&" || t === "F!" || t === "F") break;
				if (t === "/$" || t === "/&") return null;
			}
		}
		return e;
	}
	var lf = null;
	function uf(e) {
		e = e.nextSibling;
		for (var t = 0; e;) {
			if (e.nodeType === 8) {
				var n = e.data;
				if (n === "/$" || n === "/&") {
					if (t === 0) return cf(e.nextSibling);
					t--;
				} else n !== "$" && n !== "$!" && n !== "$?" && n !== "$~" && n !== "&" || t++;
			}
			e = e.nextSibling;
		}
		return null;
	}
	function df(e) {
		e = e.previousSibling;
		for (var t = 0; e;) {
			if (e.nodeType === 8) {
				var n = e.data;
				if (n === "$" || n === "$!" || n === "$?" || n === "$~" || n === "&") {
					if (t === 0) return e;
					t--;
				} else n !== "/$" && n !== "/&" || t++;
			}
			e = e.previousSibling;
		}
		return null;
	}
	function ff(e, t, n) {
		switch (t = Bd(n), e) {
			case "html":
				if (e = t.documentElement, !e) throw Error(i(452));
				return e;
			case "head":
				if (e = t.head, !e) throw Error(i(453));
				return e;
			case "body":
				if (e = t.body, !e) throw Error(i(454));
				return e;
			default: throw Error(i(451));
		}
	}
	function pf(e) {
		for (var t = e.attributes; t.length;) e.removeAttributeNode(t[0]);
		St(e);
	}
	var mf = /* @__PURE__ */ new Map(), hf = /* @__PURE__ */ new Set();
	function gf(e) {
		return typeof e.getRootNode == "function" ? e.getRootNode() : e.nodeType === 9 ? e : e.ownerDocument;
	}
	var _f = k.d;
	k.d = {
		f: vf,
		r: yf,
		D: Sf,
		C: Cf,
		L: wf,
		m: Tf,
		X: Df,
		S: Ef,
		M: Of
	};
	function vf() {
		var e = _f.f(), t = vu();
		return e || t;
	}
	function yf(e) {
		var t = wt(e);
		t !== null && t.tag === 5 && t.type === "form" ? vs(t) : _f.r(e);
	}
	var bf = typeof document > "u" ? null : document;
	function xf(e, t, n) {
		var r = bf;
		if (r && typeof t == "string" && t) {
			var i = Wt(t);
			i = "link[rel=\"" + e + "\"][href=\"" + i + "\"]", typeof n == "string" && (i += "[crossorigin=\"" + n + "\"]"), hf.has(i) || (hf.add(i), e = {
				rel: e,
				crossOrigin: n,
				href: t
			}, r.querySelector(i) === null && (t = r.createElement("link"), Pd(t, "link", e), Dt(t), r.head.appendChild(t)));
		}
	}
	function Sf(e) {
		_f.D(e), xf("dns-prefetch", e, null);
	}
	function Cf(e, t) {
		_f.C(e, t), xf("preconnect", e, t);
	}
	function wf(e, t, n) {
		_f.L(e, t, n);
		var r = bf;
		if (r && e && t) {
			var i = "link[rel=\"preload\"][as=\"" + Wt(t) + "\"]";
			t === "image" && n && n.imageSrcSet ? (i += "[imagesrcset=\"" + Wt(n.imageSrcSet) + "\"]", typeof n.imageSizes == "string" && (i += "[imagesizes=\"" + Wt(n.imageSizes) + "\"]")) : i += "[href=\"" + Wt(e) + "\"]";
			var a = i;
			switch (t) {
				case "style":
					a = Af(e);
					break;
				case "script": a = Pf(e);
			}
			mf.has(a) || (e = m({
				rel: "preload",
				href: t === "image" && n && n.imageSrcSet ? void 0 : e,
				as: t
			}, n), mf.set(a, e), r.querySelector(i) !== null || t === "style" && r.querySelector(jf(a)) || t === "script" && r.querySelector(Ff(a)) || (t = r.createElement("link"), Pd(t, "link", e), Dt(t), r.head.appendChild(t)));
		}
	}
	function Tf(e, t) {
		_f.m(e, t);
		var n = bf;
		if (n && e) {
			var r = t && typeof t.as == "string" ? t.as : "script", i = "link[rel=\"modulepreload\"][as=\"" + Wt(r) + "\"][href=\"" + Wt(e) + "\"]", a = i;
			switch (r) {
				case "audioworklet":
				case "paintworklet":
				case "serviceworker":
				case "sharedworker":
				case "worker":
				case "script": a = Pf(e);
			}
			if (!mf.has(a) && (e = m({
				rel: "modulepreload",
				href: e
			}, t), mf.set(a, e), n.querySelector(i) === null)) {
				switch (r) {
					case "audioworklet":
					case "paintworklet":
					case "serviceworker":
					case "sharedworker":
					case "worker":
					case "script": if (n.querySelector(Ff(a))) return;
				}
				r = n.createElement("link"), Pd(r, "link", e), Dt(r), n.head.appendChild(r);
			}
		}
	}
	function Ef(e, t, n) {
		_f.S(e, t, n);
		var r = bf;
		if (r && e) {
			var i = Et(r).hoistableStyles, a = Af(e);
			t ||= "default";
			var o = i.get(a);
			if (!o) {
				var s = {
					loading: 0,
					preload: null
				};
				if (o = r.querySelector(jf(a))) s.loading = 5;
				else {
					e = m({
						rel: "stylesheet",
						href: e,
						"data-precedence": t
					}, n), (n = mf.get(a)) && Rf(e, n);
					var c = o = r.createElement("link");
					Dt(c), Pd(c, "link", e), c._p = new Promise(function(e, t) {
						c.onload = e, c.onerror = t;
					}), c.addEventListener("load", function() {
						s.loading |= 1;
					}), c.addEventListener("error", function() {
						s.loading |= 2;
					}), s.loading |= 4, Lf(o, t, r);
				}
				o = {
					type: "stylesheet",
					instance: o,
					count: 1,
					state: s
				}, i.set(a, o);
			}
		}
	}
	function Df(e, t) {
		_f.X(e, t);
		var n = bf;
		if (n && e) {
			var r = Et(n).hoistableScripts, i = Pf(e), a = r.get(i);
			a || (a = n.querySelector(Ff(i)), a || (e = m({
				src: e,
				async: !0
			}, t), (t = mf.get(i)) && zf(e, t), a = n.createElement("script"), Dt(a), Pd(a, "link", e), n.head.appendChild(a)), a = {
				type: "script",
				instance: a,
				count: 1,
				state: null
			}, r.set(i, a));
		}
	}
	function Of(e, t) {
		_f.M(e, t);
		var n = bf;
		if (n && e) {
			var r = Et(n).hoistableScripts, i = Pf(e), a = r.get(i);
			a || (a = n.querySelector(Ff(i)), a || (e = m({
				src: e,
				async: !0,
				type: "module"
			}, t), (t = mf.get(i)) && zf(e, t), a = n.createElement("script"), Dt(a), Pd(a, "link", e), n.head.appendChild(a)), a = {
				type: "script",
				instance: a,
				count: 1,
				state: null
			}, r.set(i, a));
		}
	}
	function kf(e, t, n, r) {
		var a = (a = ge.current) ? gf(a) : null;
		if (!a) throw Error(i(446));
		switch (e) {
			case "meta":
			case "title": return null;
			case "style": return typeof n.precedence == "string" && typeof n.href == "string" ? (t = Af(n.href), n = Et(a).hoistableStyles, r = n.get(t), r || (r = {
				type: "style",
				instance: null,
				count: 0,
				state: null
			}, n.set(t, r)), r) : {
				type: "void",
				instance: null,
				count: 0,
				state: null
			};
			case "link":
				if (n.rel === "stylesheet" && typeof n.href == "string" && typeof n.precedence == "string") {
					e = Af(n.href);
					var o = Et(a).hoistableStyles, s = o.get(e);
					if (s || (a = a.ownerDocument || a, s = {
						type: "stylesheet",
						instance: null,
						count: 0,
						state: {
							loading: 0,
							preload: null
						}
					}, o.set(e, s), (o = a.querySelector(jf(e))) && !o._p && (s.instance = o, s.state.loading = 5), mf.has(e) || (n = {
						rel: "preload",
						as: "style",
						href: n.href,
						crossOrigin: n.crossOrigin,
						integrity: n.integrity,
						media: n.media,
						hrefLang: n.hrefLang,
						referrerPolicy: n.referrerPolicy
					}, mf.set(e, n), o || Nf(a, e, n, s.state))), t && r === null) throw Error(i(528, ""));
					return s;
				}
				if (t && r !== null) throw Error(i(529, ""));
				return null;
			case "script": return t = n.async, n = n.src, typeof n == "string" && t && typeof t != "function" && typeof t != "symbol" ? (t = Pf(n), n = Et(a).hoistableScripts, r = n.get(t), r || (r = {
				type: "script",
				instance: null,
				count: 0,
				state: null
			}, n.set(t, r)), r) : {
				type: "void",
				instance: null,
				count: 0,
				state: null
			};
			default: throw Error(i(444, e));
		}
	}
	function Af(e) {
		return "href=\"" + Wt(e) + "\"";
	}
	function jf(e) {
		return "link[rel=\"stylesheet\"][" + e + "]";
	}
	function Mf(e) {
		return m({}, e, {
			"data-precedence": e.precedence,
			precedence: null
		});
	}
	function Nf(e, t, n, r) {
		e.querySelector("link[rel=\"preload\"][as=\"style\"][" + t + "]") ? r.loading = 1 : (t = e.createElement("link"), r.preload = t, t.addEventListener("load", function() {
			return r.loading |= 1;
		}), t.addEventListener("error", function() {
			return r.loading |= 2;
		}), Pd(t, "link", n), Dt(t), e.head.appendChild(t));
	}
	function Pf(e) {
		return "[src=\"" + Wt(e) + "\"]";
	}
	function Ff(e) {
		return "script[async]" + e;
	}
	function If(e, t, n) {
		if (t.count++, t.instance === null) switch (t.type) {
			case "style":
				var r = e.querySelector("style[data-href~=\"" + Wt(n.href) + "\"]");
				if (r) return t.instance = r, Dt(r), r;
				var a = m({}, n, {
					"data-href": n.href,
					"data-precedence": n.precedence,
					href: null,
					precedence: null
				});
				return r = (e.ownerDocument || e).createElement("style"), Dt(r), Pd(r, "style", a), Lf(r, n.precedence, e), t.instance = r;
			case "stylesheet":
				a = Af(n.href);
				var o = e.querySelector(jf(a));
				if (o) return t.state.loading |= 4, t.instance = o, Dt(o), o;
				r = Mf(n), (a = mf.get(a)) && Rf(r, a), o = (e.ownerDocument || e).createElement("link"), Dt(o);
				var s = o;
				return s._p = new Promise(function(e, t) {
					s.onload = e, s.onerror = t;
				}), Pd(o, "link", r), t.state.loading |= 4, Lf(o, n.precedence, e), t.instance = o;
			case "script": return o = Pf(n.src), (a = e.querySelector(Ff(o))) ? (t.instance = a, Dt(a), a) : (r = n, (a = mf.get(o)) && (r = m({}, n), zf(r, a)), e = e.ownerDocument || e, a = e.createElement("script"), Dt(a), Pd(a, "link", r), e.head.appendChild(a), t.instance = a);
			case "void": return null;
			default: throw Error(i(443, t.type));
		}
		else t.type === "stylesheet" && !(t.state.loading & 4) && (r = t.instance, t.state.loading |= 4, Lf(r, n.precedence, e));
		return t.instance;
	}
	function Lf(e, t, n) {
		for (var r = n.querySelectorAll("link[rel=\"stylesheet\"][data-precedence],style[data-precedence]"), i = r.length ? r[r.length - 1] : null, a = i, o = 0; o < r.length; o++) {
			var s = r[o];
			if (s.dataset.precedence === t) a = s;
			else if (a !== i) break;
		}
		a ? a.parentNode.insertBefore(e, a.nextSibling) : (t = n.nodeType === 9 ? n.head : n, t.insertBefore(e, t.firstChild));
	}
	function Rf(e, t) {
		e.crossOrigin ??= t.crossOrigin, e.referrerPolicy ??= t.referrerPolicy, e.title ??= t.title;
	}
	function zf(e, t) {
		e.crossOrigin ??= t.crossOrigin, e.referrerPolicy ??= t.referrerPolicy, e.integrity ??= t.integrity;
	}
	var Bf = null;
	function Vf(e, t, n) {
		if (Bf === null) {
			var r = /* @__PURE__ */ new Map(), i = Bf = /* @__PURE__ */ new Map();
			i.set(n, r);
		} else i = Bf, r = i.get(n), r || (r = /* @__PURE__ */ new Map(), i.set(n, r));
		if (r.has(e)) return r;
		for (r.set(e, null), n = n.getElementsByTagName(e), i = 0; i < n.length; i++) {
			var a = n[i];
			if (!(a[xt] || a[mt] || e === "link" && a.getAttribute("rel") === "stylesheet") && a.namespaceURI !== "http://www.w3.org/2000/svg") {
				var o = a.getAttribute(t) || "";
				o = e + o;
				var s = r.get(o);
				s ? s.push(a) : r.set(o, [a]);
			}
		}
		return r;
	}
	function Hf(e, t, n) {
		e = e.ownerDocument || e, e.head.insertBefore(n, t === "title" ? e.querySelector("head > title") : null);
	}
	function Uf(e, t, n) {
		if (n === 1 || t.itemProp != null) return !1;
		switch (e) {
			case "meta":
			case "title": return !0;
			case "style":
				if (typeof t.precedence != "string" || typeof t.href != "string" || t.href === "") break;
				return !0;
			case "link":
				if (typeof t.rel != "string" || typeof t.href != "string" || t.href === "" || t.onLoad || t.onError) break;
				switch (t.rel) {
					case "stylesheet": return e = t.disabled, typeof t.precedence == "string" && e == null;
					default: return !0;
				}
			case "script": if (t.async && typeof t.async != "function" && typeof t.async != "symbol" && !t.onLoad && !t.onError && t.src && typeof t.src == "string") return !0;
		}
		return !1;
	}
	function Wf(e) {
		return !(e.type === "stylesheet" && !(e.state.loading & 3));
	}
	function Gf(e, t, n, r) {
		if (n.type === "stylesheet" && (typeof r.media != "string" || !1 !== matchMedia(r.media).matches) && !(n.state.loading & 4)) {
			if (n.instance === null) {
				var i = Af(r.href), a = t.querySelector(jf(i));
				if (a) {
					t = a._p, typeof t == "object" && t && typeof t.then == "function" && (e.count++, e = Jf.bind(e), t.then(e, e)), n.state.loading |= 4, n.instance = a, Dt(a);
					return;
				}
				a = t.ownerDocument || t, r = Mf(r), (i = mf.get(i)) && Rf(r, i), a = a.createElement("link"), Dt(a);
				var o = a;
				o._p = new Promise(function(e, t) {
					o.onload = e, o.onerror = t;
				}), Pd(a, "link", r), n.instance = a;
			}
			e.stylesheets === null && (e.stylesheets = /* @__PURE__ */ new Map()), e.stylesheets.set(n, t), (t = n.state.preload) && !(n.state.loading & 3) && (e.count++, n = Jf.bind(e), t.addEventListener("load", n), t.addEventListener("error", n));
		}
	}
	var Kf = 0;
	function qf(e, t) {
		return e.stylesheets && e.count === 0 && Xf(e, e.stylesheets), 0 < e.count || 0 < e.imgCount ? function(n) {
			var r = setTimeout(function() {
				if (e.stylesheets && Xf(e, e.stylesheets), e.unsuspend) {
					var t = e.unsuspend;
					e.unsuspend = null, t();
				}
			}, 6e4 + t);
			0 < e.imgBytes && Kf === 0 && (Kf = 62500 * Ld());
			var i = setTimeout(function() {
				if (e.waitingForImages = !1, e.count === 0 && (e.stylesheets && Xf(e, e.stylesheets), e.unsuspend)) {
					var t = e.unsuspend;
					e.unsuspend = null, t();
				}
			}, (e.imgBytes > Kf ? 50 : 800) + t);
			return e.unsuspend = n, function() {
				e.unsuspend = null, clearTimeout(r), clearTimeout(i);
			};
		} : null;
	}
	function Jf() {
		if (this.count--, this.count === 0 && (this.imgCount === 0 || !this.waitingForImages)) {
			if (this.stylesheets) Xf(this, this.stylesheets);
			else if (this.unsuspend) {
				var e = this.unsuspend;
				this.unsuspend = null, e();
			}
		}
	}
	var Yf = null;
	function Xf(e, t) {
		e.stylesheets = null, e.unsuspend !== null && (e.count++, Yf = /* @__PURE__ */ new Map(), t.forEach(Zf, e), Yf = null, Jf.call(e));
	}
	function Zf(e, t) {
		if (!(t.state.loading & 4)) {
			var n = Yf.get(e);
			if (n) var r = n.get(null);
			else {
				n = /* @__PURE__ */ new Map(), Yf.set(e, n);
				for (var i = e.querySelectorAll("link[data-precedence],style[data-precedence]"), a = 0; a < i.length; a++) {
					var o = i[a];
					(o.nodeName === "LINK" || o.getAttribute("media") !== "not all") && (n.set(o.dataset.precedence, o), r = o);
				}
				r && n.set(null, r);
			}
			i = t.instance, o = i.getAttribute("data-precedence"), a = n.get(o) || r, a === r && n.set(null, i), n.set(o, i), this.count++, r = Jf.bind(this), i.addEventListener("load", r), i.addEventListener("error", r), a ? a.parentNode.insertBefore(i, a.nextSibling) : (e = e.nodeType === 9 ? e.head : e, e.insertBefore(i, e.firstChild)), t.state.loading |= 4;
		}
	}
	var Qf = {
		$$typeof: C,
		Provider: null,
		Consumer: null,
		_currentValue: ce,
		_currentValue2: ce,
		_threadCount: 0
	};
	function $f(e, t, n, r, i, a, o, s, c) {
		this.tag = 1, this.containerInfo = e, this.pingCache = this.current = this.pendingChildren = null, this.timeoutHandle = -1, this.callbackNode = this.next = this.pendingContext = this.context = this.cancelPendingCommit = null, this.callbackPriority = 0, this.expirationTimes = rt(-1), this.entangledLanes = this.shellSuspendCounter = this.errorRecoveryDisabledLanes = this.expiredLanes = this.warmLanes = this.pingedLanes = this.suspendedLanes = this.pendingLanes = 0, this.entanglements = rt(0), this.hiddenUpdates = rt(null), this.identifierPrefix = r, this.onUncaughtError = i, this.onCaughtError = a, this.onRecoverableError = o, this.pooledCache = null, this.pooledCacheLanes = 0, this.formState = c, this.incompleteTransitions = /* @__PURE__ */ new Map();
	}
	function ep(e, t, n, r, i, a, o, s, c, l, u, d) {
		return e = new $f(e, t, n, o, c, l, u, d, s), t = 1, !0 === a && (t |= 24), a = di(3, null, null, t), e.current = a, a.stateNode = e, t = na(), t.refCount++, e.pooledCache = t, t.refCount++, a.memoizedState = {
			element: r,
			isDehydrated: n,
			cache: t
		}, Na(a), e;
	}
	function tp(e) {
		return e ? (e = li, e) : li;
	}
	function np(e, t, n, r, i, a) {
		i = tp(i), r.context === null ? r.context = i : r.pendingContext = i, r = Fa(t), r.payload = { element: n }, a = a === void 0 ? null : a, a !== null && (r.callback = a), n = Ia(e, r, t), n !== null && (pu(n, e, t), La(n, e, t));
	}
	function rp(e, t) {
		if (e = e.memoizedState, e !== null && e.dehydrated !== null) {
			var n = e.retryLane;
			e.retryLane = n !== 0 && n < t ? n : t;
		}
	}
	function ip(e, t) {
		rp(e, t), (e = e.alternate) && rp(e, t);
	}
	function ap(e) {
		if (e.tag === 13 || e.tag === 31) {
			var t = oi(e, 67108864);
			t !== null && pu(t, e, 67108864), ip(e, 67108864);
		}
	}
	function op(e) {
		if (e.tag === 13 || e.tag === 31) {
			var t = du();
			t = lt(t);
			var n = oi(e, t);
			n !== null && pu(n, e, t), ip(e, t);
		}
	}
	var sp = !0;
	function cp(e, t, n, r) {
		var i = O.T;
		O.T = null;
		var a = k.p;
		try {
			k.p = 2, up(e, t, n, r);
		} finally {
			k.p = a, O.T = i;
		}
	}
	function lp(e, t, n, r) {
		var i = O.T;
		O.T = null;
		var a = k.p;
		try {
			k.p = 8, up(e, t, n, r);
		} finally {
			k.p = a, O.T = i;
		}
	}
	function up(e, t, n, r) {
		if (sp) {
			var i = dp(r);
			if (i === null) Cd(e, t, r, fp, n), Cp(e, r);
			else if (Tp(i, e, t, n, r)) r.stopPropagation();
			else if (Cp(e, r), t & 4 && -1 < Sp.indexOf(e)) {
				for (; i !== null;) {
					var a = wt(i);
					if (a !== null) switch (a.tag) {
						case 3:
							if (a = a.stateNode, a.current.memoizedState.isDehydrated) {
								var o = Qe(a.pendingLanes);
								if (o !== 0) {
									var s = a;
									for (s.pendingLanes |= 2, s.entangledLanes |= 2; o;) {
										var c = 1 << 31 - Ge(o);
										s.entanglements[1] |= c, o &= ~c;
									}
									td(a), !(Ml & 6) && ($l = Pe() + 500, nd(0, !1));
								}
							}
							break;
						case 31:
						case 13: s = oi(a, 2), s !== null && pu(s, a, 2), vu(), ip(a, 2);
					}
					if (a = dp(r), a === null && Cd(e, t, r, fp, n), a === i) break;
					i = a;
				}
				i !== null && r.stopPropagation();
			} else Cd(e, t, r, null, n);
		}
	}
	function dp(e) {
		return e = cn(e), pp(e);
	}
	var fp = null;
	function pp(e) {
		if (fp = null, e = Ct(e), e !== null) {
			var t = o(e);
			if (t === null) e = null;
			else {
				var n = t.tag;
				if (n === 13) {
					if (e = s(t), e !== null) return e;
					e = null;
				} else if (n === 31) {
					if (e = c(t), e !== null) return e;
					e = null;
				} else if (n === 3) {
					if (t.stateNode.current.memoizedState.isDehydrated) return t.tag === 3 ? t.stateNode.containerInfo : null;
					e = null;
				} else t !== e && (e = null);
			}
		}
		return fp = e, null;
	}
	function mp(e) {
		switch (e) {
			case "beforetoggle":
			case "cancel":
			case "click":
			case "close":
			case "contextmenu":
			case "copy":
			case "cut":
			case "auxclick":
			case "dblclick":
			case "dragend":
			case "dragstart":
			case "drop":
			case "focusin":
			case "focusout":
			case "input":
			case "invalid":
			case "keydown":
			case "keypress":
			case "keyup":
			case "mousedown":
			case "mouseup":
			case "paste":
			case "pause":
			case "play":
			case "pointercancel":
			case "pointerdown":
			case "pointerup":
			case "ratechange":
			case "reset":
			case "resize":
			case "seeked":
			case "submit":
			case "toggle":
			case "touchcancel":
			case "touchend":
			case "touchstart":
			case "volumechange":
			case "change":
			case "selectionchange":
			case "textInput":
			case "compositionstart":
			case "compositionend":
			case "compositionupdate":
			case "beforeblur":
			case "afterblur":
			case "beforeinput":
			case "blur":
			case "fullscreenchange":
			case "focus":
			case "hashchange":
			case "popstate":
			case "select":
			case "selectstart": return 2;
			case "drag":
			case "dragenter":
			case "dragexit":
			case "dragleave":
			case "dragover":
			case "mousemove":
			case "mouseout":
			case "mouseover":
			case "pointermove":
			case "pointerout":
			case "pointerover":
			case "scroll":
			case "touchmove":
			case "wheel":
			case "mouseenter":
			case "mouseleave":
			case "pointerenter":
			case "pointerleave": return 8;
			case "message": switch (Fe()) {
				case Ie: return 2;
				case Le: return 8;
				case Re:
				case ze: return 32;
				case A: return 268435456;
				default: return 32;
			}
			default: return 32;
		}
	}
	var hp = !1, gp = null, _p = null, vp = null, yp = /* @__PURE__ */ new Map(), bp = /* @__PURE__ */ new Map(), xp = [], Sp = "mousedown mouseup touchcancel touchend touchstart auxclick dblclick pointercancel pointerdown pointerup dragend dragstart drop compositionend compositionstart keydown keypress keyup input textInput copy cut paste click change contextmenu reset".split(" ");
	function Cp(e, t) {
		switch (e) {
			case "focusin":
			case "focusout":
				gp = null;
				break;
			case "dragenter":
			case "dragleave":
				_p = null;
				break;
			case "mouseover":
			case "mouseout":
				vp = null;
				break;
			case "pointerover":
			case "pointerout":
				yp.delete(t.pointerId);
				break;
			case "gotpointercapture":
			case "lostpointercapture": bp.delete(t.pointerId);
		}
	}
	function wp(e, t, n, r, i, a) {
		return e === null || e.nativeEvent !== a ? (e = {
			blockedOn: t,
			domEventName: n,
			eventSystemFlags: r,
			nativeEvent: a,
			targetContainers: [i]
		}, t !== null && (t = wt(t), t !== null && ap(t)), e) : (e.eventSystemFlags |= r, t = e.targetContainers, i !== null && t.indexOf(i) === -1 && t.push(i), e);
	}
	function Tp(e, t, n, r, i) {
		switch (t) {
			case "focusin": return gp = wp(gp, e, t, n, r, i), !0;
			case "dragenter": return _p = wp(_p, e, t, n, r, i), !0;
			case "mouseover": return vp = wp(vp, e, t, n, r, i), !0;
			case "pointerover":
				var a = i.pointerId;
				return yp.set(a, wp(yp.get(a) || null, e, t, n, r, i)), !0;
			case "gotpointercapture": return a = i.pointerId, bp.set(a, wp(bp.get(a) || null, e, t, n, r, i)), !0;
		}
		return !1;
	}
	function Ep(e) {
		var t = Ct(e.target);
		if (t !== null) {
			var n = o(t);
			if (n !== null) {
				if (t = n.tag, t === 13) {
					if (t = s(n), t !== null) {
						e.blockedOn = t, ft(e.priority, function() {
							op(n);
						});
						return;
					}
				} else if (t === 31) {
					if (t = c(n), t !== null) {
						e.blockedOn = t, ft(e.priority, function() {
							op(n);
						});
						return;
					}
				} else if (t === 3 && n.stateNode.current.memoizedState.isDehydrated) {
					e.blockedOn = n.tag === 3 ? n.stateNode.containerInfo : null;
					return;
				}
			}
		}
		e.blockedOn = null;
	}
	function Dp(e) {
		if (e.blockedOn !== null) return !1;
		for (var t = e.targetContainers; 0 < t.length;) {
			var n = dp(e.nativeEvent);
			if (n === null) {
				n = e.nativeEvent;
				var r = new n.constructor(n.type, n);
				sn = r, n.target.dispatchEvent(r), sn = null;
			} else return t = wt(n), t !== null && ap(t), e.blockedOn = n, !1;
			t.shift();
		}
		return !0;
	}
	function Op(e, t, n) {
		Dp(e) && n.delete(t);
	}
	function kp() {
		hp = !1, gp !== null && Dp(gp) && (gp = null), _p !== null && Dp(_p) && (_p = null), vp !== null && Dp(vp) && (vp = null), yp.forEach(Op), bp.forEach(Op);
	}
	function Ap(e, n) {
		e.blockedOn === n && (e.blockedOn = null, hp || (hp = !0, t.unstable_scheduleCallback(t.unstable_NormalPriority, kp)));
	}
	var jp = null;
	function Mp(e) {
		jp !== e && (jp = e, t.unstable_scheduleCallback(t.unstable_NormalPriority, function() {
			jp === e && (jp = null);
			for (var t = 0; t < e.length; t += 3) {
				var n = e[t], r = e[t + 1], i = e[t + 2];
				if (typeof r != "function") {
					if (pp(r || n) === null) continue;
					break;
				}
				var a = wt(n);
				a !== null && (e.splice(t, 3), t -= 3, gs(a, {
					pending: !0,
					data: i,
					method: n.method,
					action: r
				}, r, i));
			}
		}));
	}
	function Np(e) {
		function t(t) {
			return Ap(t, e);
		}
		gp !== null && Ap(gp, e), _p !== null && Ap(_p, e), vp !== null && Ap(vp, e), yp.forEach(t), bp.forEach(t);
		for (var n = 0; n < xp.length; n++) {
			var r = xp[n];
			r.blockedOn === e && (r.blockedOn = null);
		}
		for (; 0 < xp.length && (n = xp[0], n.blockedOn === null);) Ep(n), n.blockedOn === null && xp.shift();
		if (n = (e.ownerDocument || e).$$reactFormReplay, n != null) for (r = 0; r < n.length; r += 3) {
			var i = n[r], a = n[r + 1], o = i[ht] || null;
			if (typeof a == "function") o || Mp(n);
			else if (o) {
				var s = null;
				if (a && a.hasAttribute("formAction")) {
					if (i = a, o = a[ht] || null) s = o.formAction;
					else if (pp(i) !== null) continue;
				} else s = o.action;
				typeof s == "function" ? n[r + 1] = s : (n.splice(r, 3), r -= 3), Mp(n);
			}
		}
	}
	function Pp() {
		function e(e) {
			e.canIntercept && e.info === "react-transition" && e.intercept({
				handler: function() {
					return new Promise(function(e) {
						return i = e;
					});
				},
				focusReset: "manual",
				scroll: "manual"
			});
		}
		function t() {
			i !== null && (i(), i = null), r || setTimeout(n, 20);
		}
		function n() {
			if (!r && !navigation.transition) {
				var e = navigation.currentEntry;
				e && e.url != null && navigation.navigate(e.url, {
					state: e.getState(),
					info: "react-transition",
					history: "replace"
				});
			}
		}
		if (typeof navigation == "object") {
			var r = !1, i = null;
			return navigation.addEventListener("navigate", e), navigation.addEventListener("navigatesuccess", t), navigation.addEventListener("navigateerror", t), setTimeout(n, 100), function() {
				r = !0, navigation.removeEventListener("navigate", e), navigation.removeEventListener("navigatesuccess", t), navigation.removeEventListener("navigateerror", t), i !== null && (i(), i = null);
			};
		}
	}
	function Fp(e) {
		this._internalRoot = e;
	}
	Ip.prototype.render = Fp.prototype.render = function(e) {
		var t = this._internalRoot;
		if (t === null) throw Error(i(409));
		var n = t.current;
		np(n, du(), e, t, null, null);
	}, Ip.prototype.unmount = Fp.prototype.unmount = function() {
		var e = this._internalRoot;
		if (e !== null) {
			this._internalRoot = null;
			var t = e.containerInfo;
			np(e.current, 2, null, e, null, null), vu(), t[gt] = null;
		}
	};
	function Ip(e) {
		this._internalRoot = e;
	}
	Ip.prototype.unstable_scheduleHydration = function(e) {
		if (e) {
			var t = dt();
			e = {
				blockedOn: null,
				target: e,
				priority: t
			};
			for (var n = 0; n < xp.length && t !== 0 && t < xp[n].priority; n++);
			xp.splice(n, 0, e), n === 0 && Ep(e);
		}
	};
	var Lp = n.version;
	if (Lp !== "19.2.6") throw Error(i(527, Lp, "19.2.6"));
	k.findDOMNode = function(e) {
		var t = e._reactInternals;
		if (t === void 0) throw typeof e.render == "function" ? Error(i(188)) : (e = Object.keys(e).join(","), Error(i(268, e)));
		return e = u(t), e = e === null ? null : f(e), e = e === null ? null : e.stateNode, e;
	};
	var Rp = {
		bundleType: 0,
		version: "19.2.6",
		rendererPackageName: "react-dom",
		currentDispatcherRef: O,
		reconcilerVersion: "19.2.6"
	};
	if (typeof __REACT_DEVTOOLS_GLOBAL_HOOK__ < "u") {
		var zp = __REACT_DEVTOOLS_GLOBAL_HOOK__;
		if (!zp.isDisabled && zp.supportsFiber) try {
			He = zp.inject(Rp), Ue = zp;
		} catch {}
	}
	e.createRoot = function(e, t) {
		if (!a(e)) throw Error(i(299));
		var n = !1, r = "", o = Rs, s = zs, c = Bs;
		return t != null && (!0 === t.unstable_strictMode && (n = !0), t.identifierPrefix !== void 0 && (r = t.identifierPrefix), t.onUncaughtError !== void 0 && (o = t.onUncaughtError), t.onCaughtError !== void 0 && (s = t.onCaughtError), t.onRecoverableError !== void 0 && (c = t.onRecoverableError)), t = ep(e, 1, !1, null, null, n, r, null, o, s, c, Pp), e[gt] = t.current, xd(e), new Fp(t);
	};
})), _ = /* @__PURE__ */ o(((e, t) => {
	function n() {
		if (!(typeof __REACT_DEVTOOLS_GLOBAL_HOOK__ > "u" || typeof __REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE != "function")) try {
			__REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE(n);
		} catch (e) {
			console.error(e);
		}
	}
	n(), t.exports = g();
})), v = (...e) => e.filter((e, t, n) => !!e && e.trim() !== "" && n.indexOf(e) === t).join(" ").trim(), y = (e) => e.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase(), b = (e) => e.replace(/^([A-Z])|[\s-_]+(\w)/g, (e, t, n) => n ? n.toUpperCase() : t.toLowerCase()), x = (e) => {
	let t = b(e);
	return t.charAt(0).toUpperCase() + t.slice(1);
}, S = {
	xmlns: "http://www.w3.org/2000/svg",
	width: 24,
	height: 24,
	viewBox: "0 0 24 24",
	fill: "none",
	stroke: "currentColor",
	strokeWidth: 2,
	strokeLinecap: "round",
	strokeLinejoin: "round"
}, C = (e) => {
	for (let t in e) if (t.startsWith("aria-") || t === "role" || t === "title") return !0;
	return !1;
}, w = p(), T = (0, w.createContext)({}), E = () => (0, w.useContext)(T), ee = (0, w.forwardRef)(({ color: e, size: t, strokeWidth: n, absoluteStrokeWidth: r, className: i = "", children: a, iconNode: o, ...s }, c) => {
	let { size: l = 24, strokeWidth: u = 2, absoluteStrokeWidth: d = !1, color: f = "currentColor", className: p = "" } = E() ?? {}, m = r ?? d ? Number(n ?? u) * 24 / Number(t ?? l) : n ?? u;
	return (0, w.createElement)("svg", {
		ref: c,
		...S,
		width: t ?? l ?? S.width,
		height: t ?? l ?? S.height,
		stroke: e ?? f,
		strokeWidth: m,
		className: v("lucide", p, i),
		...!a && !C(s) && { "aria-hidden": "true" },
		...s
	}, [...o.map(([e, t]) => (0, w.createElement)(e, t)), ...Array.isArray(a) ? a : [a]]);
}), D = (e, t) => {
	let n = (0, w.forwardRef)(({ className: n, ...r }, i) => (0, w.createElement)(ee, {
		ref: i,
		iconNode: t,
		className: v(`lucide-${y(x(e))}`, `lucide-${e}`, n),
		...r
	}));
	return n.displayName = x(e), n;
}, te = D("arrow-down", [["path", {
	d: "M12 5v14",
	key: "s699le"
}], ["path", {
	d: "m19 12-7 7-7-7",
	key: "1idqje"
}]]), ne = D("arrow-right", [["path", {
	d: "M5 12h14",
	key: "1ays0h"
}], ["path", {
	d: "m12 5 7 7-7 7",
	key: "xquz4c"
}]]), re = D("book-open-text", [
	["path", {
		d: "M12 5v16",
		key: "1f6ucr"
	}],
	["path", {
		d: "M16 13h2",
		key: "weia3s"
	}],
	["path", {
		d: "M16 9h2",
		key: "1n7gjm"
	}],
	["path", {
		d: "M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z",
		key: "1fyvmf"
	}],
	["path", {
		d: "M6 13h2",
		key: "1cckiz"
	}],
	["path", {
		d: "M6 9h2",
		key: "1k7j9f"
	}]
]), ie = D("bot", [
	["path", {
		d: "M12 8V4H8",
		key: "hb8ula"
	}],
	["rect", {
		width: "16",
		height: "12",
		x: "4",
		y: "8",
		rx: "2",
		key: "enze0r"
	}],
	["path", {
		d: "M2 14h2",
		key: "vft8re"
	}],
	["path", {
		d: "M20 14h2",
		key: "4cs60a"
	}],
	["path", {
		d: "M15 13v2",
		key: "1xurst"
	}],
	["path", {
		d: "M9 13v2",
		key: "rq6x2g"
	}]
]), ae = D("braces", [["path", {
	d: "M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1",
	key: "ezmyqa"
}], ["path", {
	d: "M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1",
	key: "e1hn23"
}]]), oe = D("brain-circuit", [
	["path", {
		d: "M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z",
		key: "l5xja"
	}],
	["path", {
		d: "M9 13a4.5 4.5 0 0 0 3-4",
		key: "10igwf"
	}],
	["path", {
		d: "M6.003 5.125A3 3 0 0 0 6.401 6.5",
		key: "105sqy"
	}],
	["path", {
		d: "M3.477 10.896a4 4 0 0 1 .585-.396",
		key: "ql3yin"
	}],
	["path", {
		d: "M6 18a4 4 0 0 1-1.967-.516",
		key: "2e4loj"
	}],
	["path", {
		d: "M12 13h4",
		key: "1ku699"
	}],
	["path", {
		d: "M12 18h6a2 2 0 0 1 2 2v1",
		key: "105ag5"
	}],
	["path", {
		d: "M12 8h8",
		key: "1lhi5i"
	}],
	["path", {
		d: "M16 8V5a2 2 0 0 1 2-2",
		key: "u6izg6"
	}],
	["circle", {
		cx: "16",
		cy: "13",
		r: ".5",
		key: "ry7gng"
	}],
	["circle", {
		cx: "18",
		cy: "3",
		r: ".5",
		key: "1aiba7"
	}],
	["circle", {
		cx: "20",
		cy: "21",
		r: ".5",
		key: "yhc1fs"
	}],
	["circle", {
		cx: "20",
		cy: "8",
		r: ".5",
		key: "1e43v0"
	}]
]), se = D("check", [["path", {
	d: "M20 6 9 17l-5-5",
	key: "1gmf2c"
}]]), O = D("chevron-down", [["path", {
	d: "m6 9 6 6 6-6",
	key: "qrunsl"
}]]), k = D("chevron-right", [["path", {
	d: "m9 18 6-6-6-6",
	key: "mthhwq"
}]]), ce = D("chevron-up", [["path", {
	d: "m18 15-6-6-6 6",
	key: "153udz"
}]]), le = D("circle-stop", [["circle", {
	cx: "12",
	cy: "12",
	r: "10",
	key: "1mglay"
}], ["rect", {
	x: "9",
	y: "9",
	width: "6",
	height: "6",
	rx: "1",
	key: "1ssd4o"
}]]), ue = D("copy", [["rect", {
	width: "14",
	height: "14",
	x: "8",
	y: "8",
	rx: "2",
	ry: "2",
	key: "17jyea"
}], ["path", {
	d: "M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2",
	key: "zix9uf"
}]]), de = D("corner-down-right", [["path", {
	d: "m15 10 5 5-5 5",
	key: "qqa56n"
}], ["path", {
	d: "M4 4v7a4 4 0 0 0 4 4h12",
	key: "z08zvw"
}]]), fe = D("eye", [["path", {
	d: "M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0",
	key: "1nclc0"
}], ["circle", {
	cx: "12",
	cy: "12",
	r: "3",
	key: "1v7zrd"
}]]), pe = D("file-diff", [
	["path", {
		d: "M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z",
		key: "1oefj6"
	}],
	["path", {
		d: "M9 10h6",
		key: "9gxzsh"
	}],
	["path", {
		d: "M12 13V7",
		key: "h0r20n"
	}],
	["path", {
		d: "M9 17h6",
		key: "r8uit2"
	}]
]), me = D("file-search", [
	["path", {
		d: "M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z",
		key: "1oefj6"
	}],
	["path", {
		d: "M14 2v5a1 1 0 0 0 1 1h5",
		key: "wfsgrz"
	}],
	["circle", {
		cx: "11.5",
		cy: "14.5",
		r: "2.5",
		key: "1bq0ko"
	}],
	["path", {
		d: "M13.3 16.3 15 18",
		key: "2quom7"
	}]
]), he = D("file-text", [
	["path", {
		d: "M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z",
		key: "1oefj6"
	}],
	["path", {
		d: "M14 2v5a1 1 0 0 0 1 1h5",
		key: "wfsgrz"
	}],
	["path", {
		d: "M10 9H8",
		key: "b1mrlr"
	}],
	["path", {
		d: "M16 13H8",
		key: "t4e002"
	}],
	["path", {
		d: "M16 17H8",
		key: "z1uh3a"
	}]
]), ge = D("git-fork", [
	["circle", {
		cx: "12",
		cy: "18",
		r: "3",
		key: "1mpf1b"
	}],
	["circle", {
		cx: "6",
		cy: "6",
		r: "3",
		key: "1lh9wr"
	}],
	["circle", {
		cx: "18",
		cy: "6",
		r: "3",
		key: "1h7g24"
	}],
	["path", {
		d: "M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9",
		key: "1uq4wg"
	}],
	["path", {
		d: "M12 12v3",
		key: "158kv8"
	}]
]), _e = D("list-todo", [
	["path", {
		d: "M13 5h8",
		key: "a7qcls"
	}],
	["path", {
		d: "M13 12h8",
		key: "h98zly"
	}],
	["path", {
		d: "M13 19h8",
		key: "c3s6r1"
	}],
	["path", {
		d: "m3 17 2 2 4-4",
		key: "1jhpwq"
	}],
	["rect", {
		x: "3",
		y: "4",
		width: "6",
		height: "6",
		rx: "1",
		key: "cif1o7"
	}]
]), ve = D("loader-circle", [["path", {
	d: "M21 12a9 9 0 1 1-6.219-8.56",
	key: "13zald"
}]]), ye = D("menu", [
	["path", {
		d: "M4 5h16",
		key: "1tepv9"
	}],
	["path", {
		d: "M4 12h16",
		key: "1lakjw"
	}],
	["path", {
		d: "M4 19h16",
		key: "1djgab"
	}]
]), be = D("message-square-plus", [
	["path", {
		d: "M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z",
		key: "18887p"
	}],
	["path", {
		d: "M12 8v6",
		key: "1ib9pf"
	}],
	["path", {
		d: "M9 11h6",
		key: "1fldmi"
	}]
]), xe = D("panel-right", [["rect", {
	width: "18",
	height: "18",
	x: "3",
	y: "3",
	rx: "2",
	key: "afitv7"
}], ["path", {
	d: "M15 3v18",
	key: "14nvp0"
}]]), Se = D("play", [["path", {
	d: "M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z",
	key: "10ikf1"
}]]), Ce = D("plug-zap", [
	["path", {
		d: "M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z",
		key: "goz73y"
	}],
	["path", {
		d: "m2 22 3-3",
		key: "19mgm9"
	}],
	["path", {
		d: "M7.5 13.5 10 11",
		key: "7xgeeb"
	}],
	["path", {
		d: "M10.5 16.5 13 14",
		key: "10btkg"
	}],
	["path", {
		d: "m18 3-4 4h6l-4 4",
		key: "16psg9"
	}]
]), we = D("plus", [["path", {
	d: "M5 12h14",
	key: "1ays0h"
}], ["path", {
	d: "M12 5v14",
	key: "s699le"
}]]), Te = D("rotate-ccw", [["path", {
	d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8",
	key: "1357e3"
}], ["path", {
	d: "M3 3v5h5",
	key: "1xhq8a"
}]]), Ee = D("save", [
	["path", {
		d: "M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z",
		key: "1c8476"
	}],
	["path", {
		d: "M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7",
		key: "1ydtos"
	}],
	["path", {
		d: "M7 3v4a1 1 0 0 0 1 1h7",
		key: "t51u73"
	}]
]), De = D("send", [["path", {
	d: "M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z",
	key: "1ffxy3"
}], ["path", {
	d: "m21.854 2.147-10.94 10.939",
	key: "12cjpa"
}]]), Oe = D("shield-check", [["path", {
	d: "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z",
	key: "oel41y"
}], ["path", {
	d: "m9 12 2 2 4-4",
	key: "dzmm74"
}]]), ke = D("sliders-horizontal", [
	["path", {
		d: "M10 5H3",
		key: "1qgfaw"
	}],
	["path", {
		d: "M12 19H3",
		key: "yhmn1j"
	}],
	["path", {
		d: "M14 3v4",
		key: "1sua03"
	}],
	["path", {
		d: "M16 17v4",
		key: "1q0r14"
	}],
	["path", {
		d: "M21 12h-9",
		key: "1o4lsq"
	}],
	["path", {
		d: "M21 19h-5",
		key: "1rlt1p"
	}],
	["path", {
		d: "M21 5h-7",
		key: "1oszz2"
	}],
	["path", {
		d: "M8 10v4",
		key: "tgpxqk"
	}],
	["path", {
		d: "M8 12H3",
		key: "a7s4jb"
	}]
]), Ae = D("sparkles", [
	["path", {
		d: "M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z",
		key: "1s2grr"
	}],
	["path", {
		d: "M20 2v4",
		key: "1rf3ol"
	}],
	["path", {
		d: "M22 4h-4",
		key: "gwowj6"
	}],
	["circle", {
		cx: "4",
		cy: "20",
		r: "2",
		key: "6kqj1y"
	}]
]), je = D("square-terminal", [
	["path", {
		d: "m7 11 2-2-2-2",
		key: "1lz0vl"
	}],
	["path", {
		d: "M11 13h4",
		key: "1p7l4v"
	}],
	["rect", {
		width: "18",
		height: "18",
		x: "3",
		y: "3",
		rx: "2",
		ry: "2",
		key: "1m3agn"
	}]
]), Me = D("trash-2", [
	["path", {
		d: "M10 11v6",
		key: "nco0om"
	}],
	["path", {
		d: "M14 11v6",
		key: "outv1u"
	}],
	["path", {
		d: "M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6",
		key: "miytrc"
	}],
	["path", {
		d: "M3 6h18",
		key: "d0wm0j"
	}],
	["path", {
		d: "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
		key: "e791ji"
	}]
]), Ne = D("triangle-alert", [
	["path", {
		d: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3",
		key: "wmoenq"
	}],
	["path", {
		d: "M12 9v4",
		key: "juzpu7"
	}],
	["path", {
		d: "M12 17h.01",
		key: "p32p05"
	}]
]), Pe = D("user-round", [["circle", {
	cx: "12",
	cy: "8",
	r: "5",
	key: "1hypcn"
}], ["path", {
	d: "M20 21a8 8 0 0 0-16 0",
	key: "rfgkzh"
}]]), Fe = D("wand-sparkles", [
	["path", {
		d: "m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72",
		key: "ul74o6"
	}],
	["path", {
		d: "m14 7 3 3",
		key: "1r5n42"
	}],
	["path", {
		d: "M5 6v4",
		key: "ilb8ba"
	}],
	["path", {
		d: "M19 14v4",
		key: "blhpug"
	}],
	["path", {
		d: "M10 2v2",
		key: "7u0qdc"
	}],
	["path", {
		d: "M7 8H3",
		key: "zfb6yr"
	}],
	["path", {
		d: "M21 16h-4",
		key: "1cnmox"
	}],
	["path", {
		d: "M11 3H9",
		key: "1obp7u"
	}]
]), Ie = D("x", [["path", {
	d: "M18 6 6 18",
	key: "1bl5f8"
}], ["path", {
	d: "m6 6 12 12",
	key: "d8bk6v"
}]]), Le = D("zap", [["path", {
	d: "M15.914 4a1.5 1.5 0 00-2.474-1.561l-9 9A1.5 1.5 0 005.5 14h4.002a.5.5 0 01.471.666L8.086 20a1.5 1.5 0 002.475 1.56l9-9A1.5 1.5 0 0018.5 10h-3.997a.5.5 0 01-.472-.667z",
	key: "1v7up4"
}]]), Re = _(), ze = /* @__PURE__ */ o(((e) => {
	var t = Symbol.for("react.transitional.element"), n = Symbol.for("react.fragment");
	function r(e, n, r) {
		var i = null;
		if (r !== void 0 && (i = "" + r), n.key !== void 0 && (i = "" + n.key), "key" in n) for (var a in r = {}, n) a !== "key" && (r[a] = n[a]);
		else r = n;
		return n = r.ref, {
			$$typeof: t,
			type: e,
			key: i,
			ref: n === void 0 ? null : n,
			props: r
		};
	}
	e.Fragment = n, e.jsx = r, e.jsxs = r;
})), A = (/* @__PURE__ */ o(((e, t) => {
	t.exports = ze();
})))();
function Be(e) {
	return e.toolName === "create_agent_tasks" || e.toolName === "spawn_agent";
}
function Ve(e) {
	return typeof e == "object" && e && !Array.isArray(e) ? e : void 0;
}
function He(e) {
	try {
		return Ve(JSON.parse(e ?? ""));
	} catch {
		return;
	}
}
function Ue(e) {
	if (!Be(e) || e.resultState !== "SUCCESS") return;
	let t = He(e.argumentsJson), n = He(e.resultText);
	if (e.toolName === "spawn_agent" && typeof n?.task_id == "string") return {
		names: [typeof t?.task_name == "string" ? t.task_name : "子任务"],
		target: {
			kind: "task",
			id: n.task_id
		}
	};
	if (e.toolName !== "create_agent_tasks" || typeof n?.batch_id != "string" || !Array.isArray(n.tasks) || n.tasks.length === 0) return;
	let r = Array.isArray(t?.tasks) ? t.tasks.map(Ve) : [];
	return {
		names: n.tasks.map((e) => {
			let t = Ve(e), n = r.find((e) => e && typeof t?.task_key == "string" && e.task_key === t.task_key);
			return typeof n?.label == "string" ? n.label : typeof t?.task_key == "string" ? t.task_key : "子任务";
		}),
		target: {
			kind: "batch",
			id: n.batch_id
		}
	};
}
function We({ trace: e }) {
	let t = Ue(e), [n, r] = (0, w.useState)(!1), [i, a] = (0, w.useState)(!1), o = async () => {
		try {
			await navigator.clipboard.writeText(e.resultText ?? ""), r(!0), a(!1);
		} catch {
			a(!0);
		}
	};
	return /* @__PURE__ */ (0, A.jsxs)("section", {
		className: "demo-task-result",
		"aria-label": "子任务创建摘要",
		children: [t && /* @__PURE__ */ (0, A.jsxs)("button", {
			type: "button",
			className: "demo-task-result__jump",
			"aria-label": `在右侧侧栏查看这 ${t.names.length} 个子任务`,
			onClick: () => window.parent.postMessage({
				type: "pulsara-demo-focus-tasks",
				target: t.target
			}, location.origin),
			children: [/* @__PURE__ */ (0, A.jsxs)("span", {
				className: "demo-task-result__heading",
				children: [/* @__PURE__ */ (0, A.jsxs)("strong", { children: [t.names.length, " 个子任务"] }), /* @__PURE__ */ (0, A.jsxs)("span", {
					className: "demo-task-result__link",
					children: ["在侧栏查看 ", /* @__PURE__ */ (0, A.jsx)(ne, { size: 14 })]
				})]
			}), /* @__PURE__ */ (0, A.jsx)("span", {
				className: "demo-task-result__names",
				children: t.names.map((e, t) => /* @__PURE__ */ (0, A.jsxs)("span", {
					className: "demo-task-result__name",
					children: [/* @__PURE__ */ (0, A.jsx)("span", {
						className: "demo-task-result__number",
						children: String(t + 1).padStart(2, "0")
					}), /* @__PURE__ */ (0, A.jsx)("span", { children: e })]
				}, t))
			})]
		}), Object.prototype.hasOwnProperty.call(e, "resultText") && /* @__PURE__ */ (0, A.jsxs)("details", {
			className: "demo-task-result__raw",
			open: t ? void 0 : !0,
			children: [/* @__PURE__ */ (0, A.jsxs)("summary", { children: [/* @__PURE__ */ (0, A.jsx)(k, { size: 12 }), "原始结果"] }), /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "demo-task-result__raw-body",
				children: [
					/* @__PURE__ */ (0, A.jsxs)("button", {
						type: "button",
						onClick: () => void o(),
						"aria-label": "复制工具原始结果",
						children: [n ? /* @__PURE__ */ (0, A.jsx)(se, { size: 12 }) : /* @__PURE__ */ (0, A.jsx)(ue, { size: 12 }), n ? "已复制" : "复制"]
					}),
					i && /* @__PURE__ */ (0, A.jsx)("span", {
						role: "status",
						children: "复制失败，请重试"
					}),
					/* @__PURE__ */ (0, A.jsx)("pre", { children: e.resultText === "" ? "（空字符串）" : e.resultText })
				]
			})]
		})]
	});
}
//#endregion
//#region frontend/components/mcp-editor.tsx
var Ge = {
	none: "无需认证",
	bearer: "Bearer Token",
	static_headers: "秘密 Header",
	oauth: "浏览器登录"
}, Ke = (e) => e && typeof e == "object" && !Array.isArray(e) ? e : {}, qe = (e) => JSON.stringify(e ?? {}, null, 2);
function Je(e) {
	let t = JSON.parse(e);
	if (!t || typeof t != "object" || Array.isArray(t) || Object.values(t).some((e) => typeof e != "string")) throw Error("请使用名称与文字值组成的 JSON 对象。");
	return t;
}
function Ye({ server: e, onClose: t, onSave: n, onTest: r, onAuthorization: i, onRestoreDefaults: a, credentialOwner: o, credentialScopeKey: s, packageDefinition: c, connectionInputs: l = [] }) {
	let [u, d] = (0, w.useState)({}), [f, p] = (0, w.useState)({}), m = e?.config ?? {}, [h, g] = (0, w.useState)(!1), _ = Ke(m.transport), v = Ke(m.auth), [y, b] = (0, w.useState)(e?.id ?? ""), [x, S] = (0, w.useState)(e?.name ?? ""), [C, T] = (0, w.useState)(String(_.type ?? "streamable_http")), [E, ee] = (0, w.useState)(String(_.endpoint ?? "")), [D, te] = (0, w.useState)(String(_.command ?? "")), [ne, re] = (0, w.useState)(JSON.stringify(_.args ?? [])), [ie, ae] = (0, w.useState)(String(_.cwd ?? ".")), [oe, se] = (0, w.useState)(qe(_.env)), [k, ce] = (0, w.useState)(qe(m.public_headers)), [le, ue] = (0, w.useState)(String(v.type ?? "none")), [de, fe] = (0, w.useState)(""), [pe, me] = (0, w.useState)(Ke(v.reference).source === "environment" ? "environment" : "managed"), [he, ge] = (0, w.useState)(String(Ke(v.reference).name ?? "")), [_e, ye] = (0, w.useState)(!1), [be, xe] = (0, w.useState)(Object.entries(Ke(v.headers)).map(([e, t]) => ({
		name: e,
		reference: t,
		value: "",
		clear: !1
	}))), [Se, Te] = (0, w.useState)(Object.entries(Ke(_.secret_env)).map(([e, t]) => ({
		name: e,
		reference: t,
		value: "",
		clear: !1
	}))), [De, Oe] = (0, w.useState)(String(v.client_id ?? "")), [ke, Ae] = (0, w.useState)(""), [je, Ne] = (0, w.useState)(v.client_secret ? Ke(v.client_secret).source === "environment" ? "environment" : "managed" : "none"), [Pe, Fe] = (0, w.useState)(String(Ke(v.client_secret).name ?? "")), [Le, Re] = (0, w.useState)(!1), [ze, Be] = (0, w.useState)(String(v.scope ?? "")), [Ve, He] = (0, w.useState)(String(v.redirect_uri ?? "http://127.0.0.1:17839/callback")), [Ue, We] = (0, w.useState)(String(v.resource ?? "")), [Ge, Ye] = (0, w.useState)(String(v.client_metadata_url ?? "")), [Xe, Ze] = (0, w.useState)(e?.enabled ?? !0), [Qe, $e] = (0, w.useState)(e?.availableToSubagents ?? !1), [et, tt] = (0, w.useState)(!!_.allow_http_localhost), [nt, rt] = (0, w.useState)(String(_.network_policy ?? "PUBLIC_ONLY")), [it, at] = (0, w.useState)(!!_.proved_stateless), [ot, st] = (0, w.useState)({}), [ct, lt] = (0, w.useState)(), [ut, dt] = (0, w.useState)(), ft = {
		...m,
		...ot
	}, pt = Ke(ft.exposure_policy), mt = Ke(ft.effect_policy), ht = (e, t) => st((n) => ({
		...n,
		[e]: t
	})), gt = (e, t) => ht("exposure_policy", {
		...pt,
		[e]: t
	}), [_t, vt] = (0, w.useState)(!1), [yt, bt] = (0, w.useState)(!1), [xt, St] = (0, w.useState)(""), [Ct, wt] = (0, w.useState)("");
	(0, w.useEffect)(() => {
		let e = (e) => {
			e.key === "Escape" && !yt && t();
		};
		return window.addEventListener("keydown", e), () => window.removeEventListener("keydown", e);
	}, [yt, t]);
	let Tt = !!(e && (C !== _.type || (C === "stdio" ? D !== _.command || ne !== JSON.stringify(_.args ?? []) || ie !== String(_.cwd ?? ".") : E !== _.endpoint))), Et = pe === "environment" ? Ke(v.reference).source === "environment" && Ke(v.reference).name === he : !!(v.reference && Ke(v.reference).source !== "environment" && !de && !_e), Dt = je === "environment" ? Ke(v.client_secret).source === "environment" && Ke(v.client_secret).name === Pe : je === "managed" && !!(v.client_secret && Ke(v.client_secret).source !== "environment" && !ke && !Le), Ot = C === "stdio" ? Se.some((e) => e.reference && !e.value && !e.clear) : le === "bearer" ? Et : le === "static_headers" ? be.some((e) => e.reference && !e.value && !e.clear) : le === "oauth" && Dt, kt = async (e = !1) => {
		if (!yt) {
			St(""), bt(!0);
			try {
				let i = [], a = (e, t, n, r) => {
					if (r && !t && !n) return r;
					let a = {
						owner: o ?? {
							kind: "local",
							scope_key: s ?? "user",
							server_id: y.trim(),
							plugin_id: null
						},
						name: e
					};
					return (t || n) && i.push({
						binding: a,
						value: n ? null : t
					}), {
						source: "managed",
						binding: a
					};
				}, c = (e, t) => {
					if (new Set(e.map((e) => e.name.toLowerCase())).size !== e.length) throw Error("名称不能重复。");
					return Object.fromEntries(e.map((e) => [e.name, (e.source ?? Ke(e.reference).source) === "environment" ? {
						source: "environment",
						name: e.environmentName ?? Ke(e.reference).name
					} : a(t + e.name, e.value, e.clear, e.reference)]));
				};
				for (let e of l) e.private && (u[e.name] || f[e.name]) && a("input:" + e.name, u[e.name] ?? "", !!f[e.name]);
				let d = { type: "none" };
				C !== "stdio" && (le === "bearer" && (d = {
					type: le,
					reference: pe === "environment" ? {
						source: "environment",
						name: he
					} : a("bearer", de, _e, v.type === le && Ke(v.reference).source !== "environment" ? v.reference : void 0)
				}), le === "static_headers" && (d = {
					type: le,
					headers: c(be, "header:")
				}), le === "oauth" && (d = {
					type: le,
					client_id: De || null,
					client_secret: je === "none" ? null : je === "environment" ? {
						source: "environment",
						name: Pe
					} : a("oauth-client-secret", ke, Le, Ke(v.client_secret).source === "environment" ? void 0 : v.client_secret),
					scope: ze || null,
					redirect_uri: Ve,
					resource: Ue || null,
					client_metadata_url: Ge || null
				}));
				let p = JSON.parse(ne);
				if (!Array.isArray(p) || p.some((e) => typeof e != "string")) throw Error("参数需要是字符串数组，例如 [\"-y\", \"包名\"]。");
				let h = C === "stdio" ? {
					type: C,
					command: D,
					args: p,
					cwd: ie,
					env: Je(oe),
					secret_env: c(Se, "env:")
				} : {
					..._.type === C ? _ : {},
					type: C,
					endpoint: E,
					allow_http_localhost: et,
					network_policy: nt,
					proved_stateless: C === "streamable_http" && it
				}, g = { ...ot };
				if (ct !== void 0) {
					let e = JSON.parse(ct);
					if (!e || typeof e != "object" || Array.isArray(e) || Object.values(e).some((e) => typeof e != "number" || !Number.isInteger(e))) throw Error("工具超时需要是工具名与整数毫秒组成的 JSON 对象。");
					g.per_tool_timeout_ms = e;
				}
				ut !== void 0 && (g.effect_policy = {
					...mt,
					tool_effect_overrides: Je(ut)
				});
				let b = {
					serverId: y.trim(),
					retainCredentialsConfirmed: _t,
					config: {
						...m,
						...g,
						display_name: x.trim() || y.trim(),
						enabled: Xe,
						transport: h,
						auth: d,
						public_headers: C === "stdio" ? {} : Je(k),
						scope_policy: Qe ? "ROOT_AND_SUBAGENTS" : "ROOT_ONLY"
					},
					secretChanges: i
				};
				if (e && r) {
					wt("");
					let e = await r(b);
					wt(e.status === "ready" ? `连接正常：${e.tools} 个工具、${e.resources} 项资源、${e.resource_templates} 个资源模板、${e.prompts} 个提示词。未保存或启用。` : {
						credential_required: "请填写连接凭据。",
						authorization_required: "请先保存配置并登录授权。",
						timeout: "连接或目录读取超时；仍可保存后再试。",
						schema_bound_exceeded: "已连接服务，但工具定义超出资源限制，无法采用完整工具目录；不是登录授权失败。仍可保存配置。",
						failed: "连接或目录读取失败，请检查地址、认证和协议；仍可保存。"
					}[e.status]);
				} else await n(b) && t();
			} catch (e) {
				St(e instanceof Error ? e.message : "配置无法保存，请检查填写内容。");
			} finally {
				bt(!1);
			}
		}
	}, At = (e, t, n) => /* @__PURE__ */ (0, A.jsxs)("div", {
		className: "capability-field capability-field--wide",
		children: [
			/* @__PURE__ */ (0, A.jsx)("span", { children: n }),
			e.map((r, i) => /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "mcp-secret-row",
				children: [
					/* @__PURE__ */ (0, A.jsx)("input", {
						"aria-label": `${n}名称 ${i + 1}`,
						value: r.name,
						onChange: (n) => t(e.map((e, t) => t === i ? {
							...e,
							name: n.target.value,
							reference: void 0
						} : e)),
						placeholder: "名称"
					}),
					/* @__PURE__ */ (0, A.jsxs)("select", {
						"aria-label": `${n}来源 ${i + 1}`,
						value: r.source ?? (Ke(r.reference).source === "environment" ? "environment" : "managed"),
						onChange: (n) => t(e.map((e, t) => t === i ? {
							...e,
							source: n.target.value,
							reference: void 0,
							value: "",
							clear: !1
						} : e)),
						children: [/* @__PURE__ */ (0, A.jsx)("option", {
							value: "managed",
							children: "本机保存"
						}), /* @__PURE__ */ (0, A.jsx)("option", {
							value: "environment",
							children: "Host 环境变量"
						})]
					}),
					(r.source ?? Ke(r.reference).source) === "environment" ? /* @__PURE__ */ (0, A.jsx)("input", {
						"aria-label": `${n}环境变量名 ${i + 1}`,
						value: r.environmentName ?? String(Ke(r.reference).name ?? ""),
						onChange: (n) => t(e.map((e, t) => t === i ? {
							...e,
							environmentName: n.target.value
						} : e)),
						placeholder: "Host 启动时的环境变量名"
					}) : /* @__PURE__ */ (0, A.jsx)("input", {
						type: "password",
						autoComplete: "new-password",
						"aria-label": `${n}值 ${i + 1}`,
						value: r.value,
						onChange: (n) => t(e.map((e, t) => t === i ? {
							...e,
							value: n.target.value,
							clear: !1
						} : e)),
						placeholder: r.reference ? "留空保留已保存的值" : "秘密值（仅保存到本机）"
					}),
					/* @__PURE__ */ (0, A.jsx)("button", {
						type: "button",
						className: "danger-ghost",
						"aria-label": `移除${n} ${i + 1}`,
						onClick: () => t(e.filter((e, t) => t !== i)),
						children: /* @__PURE__ */ (0, A.jsx)(Me, { size: 14 })
					})
				]
			}, i)),
			/* @__PURE__ */ (0, A.jsxs)("button", {
				className: "secondary-ghost",
				type: "button",
				onClick: () => t([...e, {
					name: "",
					value: "",
					clear: !1
				}]),
				children: [/* @__PURE__ */ (0, A.jsx)(we, { size: 13 }), "添加一项"]
			})
		]
	});
	return /* @__PURE__ */ (0, A.jsxs)("div", {
		className: "capability-add-panel mcp-editor",
		role: "dialog",
		"aria-modal": "true",
		"aria-label": e ? "编辑 MCP 服务" : "添加 MCP 服务",
		children: [/* @__PURE__ */ (0, A.jsx)("div", {
			className: "capability-add-panel__backdrop",
			onClick: yt ? void 0 : t
		}), /* @__PURE__ */ (0, A.jsxs)("section", { children: [
			/* @__PURE__ */ (0, A.jsxs)("header", { children: [/* @__PURE__ */ (0, A.jsxs)("div", { children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "本机连接 · 凭据不会交给模型" }), /* @__PURE__ */ (0, A.jsx)("h2", { children: e ? "编辑 MCP 服务" : "添加 MCP 服务" })] }), /* @__PURE__ */ (0, A.jsx)("button", {
				disabled: yt,
				onClick: t,
				"aria-label": "关闭",
				children: /* @__PURE__ */ (0, A.jsx)(Ie, { size: 16 })
			})] }),
			/* @__PURE__ */ (0, A.jsxs)("div", {
				className: "capability-form-grid capability-dialog-body",
				children: [
					/* @__PURE__ */ (0, A.jsxs)("label", {
						className: "capability-field",
						children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "服务 ID" }), /* @__PURE__ */ (0, A.jsx)("input", {
							autoFocus: !0,
							disabled: !!e,
							value: y,
							onChange: (e) => b(e.target.value)
						})]
					}),
					/* @__PURE__ */ (0, A.jsxs)("label", {
						className: "capability-field",
						children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "显示名称" }), /* @__PURE__ */ (0, A.jsx)("input", {
							disabled: !!c,
							value: x,
							onChange: (e) => S(e.target.value)
						})]
					}),
					/* @__PURE__ */ (0, A.jsxs)("label", {
						className: "capability-field capability-field--wide",
						children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "连接方式" }), /* @__PURE__ */ (0, A.jsxs)("select", {
							disabled: !!c,
							value: C,
							onChange: (e) => T(e.target.value),
							children: [
								/* @__PURE__ */ (0, A.jsx)("option", {
									value: "streamable_http",
									children: "Streamable HTTP"
								}),
								/* @__PURE__ */ (0, A.jsx)("option", {
									value: "sse",
									children: "SSE"
								}),
								/* @__PURE__ */ (0, A.jsx)("option", {
									value: "stdio",
									children: "本地命令"
								})
							]
						})]
					}),
					c && /* @__PURE__ */ (0, A.jsxs)("details", {
						className: "capability-field capability-field--wide",
						open: h || void 0,
						children: [
							/* @__PURE__ */ (0, A.jsx)("summary", { children: "插件默认连接（只读）" }),
							/* @__PURE__ */ (0, A.jsx)("pre", { children: qe(c) }),
							/* @__PURE__ */ (0, A.jsx)("small", { children: "保存仅覆盖本机实例；同名 Header / 环境变量会覆盖这里的默认值，不修改插件文件。" }),
							a && /* @__PURE__ */ (0, A.jsx)("div", {
								className: "capability-detail-actions",
								children: h ? /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
									/* @__PURE__ */ (0, A.jsx)("p", { children: "恢复上方默认目的地和认证设置，并清除不再使用的本机凭据与授权。不会修改插件文件或吊销远端密钥。" }),
									/* @__PURE__ */ (0, A.jsx)("button", {
										className: "secondary-ghost",
										disabled: yt,
										onClick: () => g(!1),
										children: "取消恢复"
									}),
									/* @__PURE__ */ (0, A.jsx)("button", {
										className: "danger-ghost",
										disabled: yt,
										onClick: () => void (async () => {
											bt(!0), St("");
											try {
												await a() && t();
											} catch (e) {
												St(e instanceof Error ? e.message : "未恢复默认连接。");
											} finally {
												bt(!1);
											}
										})(),
										children: "确认恢复默认连接"
									})
								] }) : /* @__PURE__ */ (0, A.jsx)("button", {
									className: "secondary-ghost",
									disabled: yt,
									onClick: () => g(!0),
									children: "恢复插件默认连接"
								})
							})
						]
					}),
					l.map((e) => e.private ? /* @__PURE__ */ (0, A.jsxs)("label", {
						className: "capability-field capability-field--wide",
						children: [
							/* @__PURE__ */ (0, A.jsxs)("span", { children: [e.title, e.required ? "（连接所需）" : "（可选）"] }),
							/* @__PURE__ */ (0, A.jsx)("input", {
								type: "password",
								autoComplete: "new-password",
								"aria-label": e.title,
								value: u[e.name] ?? "",
								placeholder: "留空保留本机值；尚未配置时可先保存",
								onChange: (t) => {
									d({
										...u,
										[e.name]: t.target.value
									}), p({
										...f,
										[e.name]: !1
									});
								}
							}),
							/* @__PURE__ */ (0, A.jsx)("button", {
								type: "button",
								className: "danger-ghost",
								onClick: () => {
									d({
										...u,
										[e.name]: ""
									}), p({
										...f,
										[e.name]: !0
									});
								},
								children: f[e.name] ? "保存时清除该值" : "清除本机值"
							})
						]
					}, e.name) : /* @__PURE__ */ (0, A.jsxs)("p", {
						className: "capability-field capability-field--wide",
						children: [
							e.title,
							"：",
							e.default,
							" ",
							/* @__PURE__ */ (0, A.jsx)("small", { children: "此普通参数已应用到下方连接字段，可直接修改对应字段。" })
						]
					}, e.name)),
					C === "stdio" ? /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
						/* @__PURE__ */ (0, A.jsxs)("label", {
							className: "capability-field capability-field--wide",
							children: [
								/* @__PURE__ */ (0, A.jsx)("span", { children: "命令" }),
								/* @__PURE__ */ (0, A.jsx)("input", {
									"aria-label": "命令",
									disabled: !!c,
									value: D,
									onChange: (e) => te(e.target.value),
									placeholder: "npx"
								}),
								/* @__PURE__ */ (0, A.jsx)("small", { children: "启用后会在本机启动这个命令，请只使用可信来源。" })
							]
						}),
						/* @__PURE__ */ (0, A.jsxs)("label", {
							className: "capability-field capability-field--wide",
							children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "参数（JSON 数组）" }), /* @__PURE__ */ (0, A.jsx)("textarea", {
								disabled: !!c,
								value: ne,
								onChange: (e) => re(e.target.value)
							})]
						}),
						/* @__PURE__ */ (0, A.jsxs)("label", {
							className: "capability-field",
							children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "工作目录（相对项目）" }), /* @__PURE__ */ (0, A.jsx)("input", {
								disabled: !!c,
								value: ie,
								onChange: (e) => ae(e.target.value)
							})]
						}),
						At(Se, Te, "秘密环境变量")
					] }) : /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
						/* @__PURE__ */ (0, A.jsxs)("label", {
							className: "capability-field capability-field--wide",
							children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "服务地址" }), /* @__PURE__ */ (0, A.jsx)("input", {
								value: E,
								onChange: (e) => ee(e.target.value),
								placeholder: "https://example.com/mcp"
							})]
						}),
						/* @__PURE__ */ (0, A.jsxs)("label", {
							className: "capability-field capability-field--wide",
							children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "认证方式" }), /* @__PURE__ */ (0, A.jsxs)("select", {
								value: le,
								onChange: (e) => ue(e.target.value),
								children: [
									/* @__PURE__ */ (0, A.jsx)("option", {
										value: "none",
										children: "无需认证"
									}),
									/* @__PURE__ */ (0, A.jsx)("option", {
										value: "bearer",
										children: "Bearer Token"
									}),
									/* @__PURE__ */ (0, A.jsx)("option", {
										value: "static_headers",
										children: "秘密 Header"
									}),
									/* @__PURE__ */ (0, A.jsx)("option", {
										value: "oauth",
										children: "浏览器登录（OAuth）"
									})
								]
							})]
						}),
						le === "bearer" && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [/* @__PURE__ */ (0, A.jsxs)("label", {
							className: "capability-field",
							children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "Token 来源" }), /* @__PURE__ */ (0, A.jsxs)("select", {
								value: pe,
								onChange: (e) => {
									me(e.target.value), fe(""), ye(!1);
								},
								children: [/* @__PURE__ */ (0, A.jsx)("option", {
									value: "managed",
									children: "本机保存"
								}), /* @__PURE__ */ (0, A.jsx)("option", {
									value: "environment",
									children: "Host 环境变量"
								})]
							})]
						}), pe === "environment" ? /* @__PURE__ */ (0, A.jsxs)("label", {
							className: "capability-field",
							children: [
								/* @__PURE__ */ (0, A.jsx)("span", { children: "Token 环境变量名" }),
								/* @__PURE__ */ (0, A.jsx)("input", {
									"aria-label": "Token 环境变量名",
									value: he,
									onChange: (e) => ge(e.target.value)
								}),
								/* @__PURE__ */ (0, A.jsx)("small", { children: "只引用 Host 启动时继承的变量；不能引用 PULSARA_API_KEY。" })
							]
						}) : /* @__PURE__ */ (0, A.jsxs)("label", {
							className: "capability-field capability-field--wide",
							children: [
								/* @__PURE__ */ (0, A.jsx)("span", { children: "Token" }),
								/* @__PURE__ */ (0, A.jsx)("input", {
									type: "password",
									autoComplete: "new-password",
									value: de,
									onChange: (e) => {
										fe(e.target.value), ye(!1);
									},
									placeholder: v.reference ? "留空保留已保存的值" : "仅保存到本机"
								}),
								!!v.reference && Ke(v.reference).source !== "environment" && /* @__PURE__ */ (0, A.jsx)("button", {
									className: "danger-ghost",
									onClick: () => {
										fe(""), ye(!0);
									},
									children: _e ? "保存时清除凭据" : "清除已保存凭据"
								})
							]
						})] }),
						le === "static_headers" && At(be, xe, "秘密 Header"),
						le === "oauth" && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
							/* @__PURE__ */ (0, A.jsx)("p", {
								className: "capability-field--wide",
								children: "先保存连接，再从服务详情中点击“登录授权”。保存不会弹出浏览器，也不会自动启用服务。"
							}),
							/* @__PURE__ */ (0, A.jsxs)("label", {
								className: "capability-field",
								children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "客户端 ID（可选）" }), /* @__PURE__ */ (0, A.jsx)("input", {
									value: De,
									onChange: (e) => Oe(e.target.value)
								})]
							}),
							/* @__PURE__ */ (0, A.jsxs)("label", {
								className: "capability-field",
								children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "客户端密钥来源" }), /* @__PURE__ */ (0, A.jsxs)("select", {
									value: je,
									onChange: (e) => {
										Ne(e.target.value), Ae(""), Re(!1);
									},
									children: [
										/* @__PURE__ */ (0, A.jsx)("option", {
											value: "none",
											children: "不使用客户端密钥"
										}),
										/* @__PURE__ */ (0, A.jsx)("option", {
											value: "managed",
											children: "本机保存"
										}),
										/* @__PURE__ */ (0, A.jsx)("option", {
											value: "environment",
											children: "Host 环境变量"
										})
									]
								})]
							}),
							je === "environment" && /* @__PURE__ */ (0, A.jsxs)("label", {
								className: "capability-field",
								children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "客户端密钥环境变量名" }), /* @__PURE__ */ (0, A.jsx)("input", {
									value: Pe,
									onChange: (e) => Fe(e.target.value)
								})]
							}),
							je === "managed" && /* @__PURE__ */ (0, A.jsxs)("label", {
								className: "capability-field",
								children: [
									/* @__PURE__ */ (0, A.jsx)("span", { children: "客户端密钥" }),
									/* @__PURE__ */ (0, A.jsx)("input", {
										type: "password",
										autoComplete: "new-password",
										value: ke,
										onChange: (e) => {
											Ae(e.target.value), Re(!1);
										},
										placeholder: v.client_secret ? "留空保留" : ""
									}),
									!!v.client_secret && /* @__PURE__ */ (0, A.jsx)("button", {
										className: "danger-ghost",
										onClick: () => {
											Ae(""), Re(!0);
										},
										children: "保存时清除密钥值"
									})
								]
							}),
							/* @__PURE__ */ (0, A.jsxs)("label", {
								className: "capability-field",
								children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "请求权限（scope，可选）" }), /* @__PURE__ */ (0, A.jsx)("input", {
									value: ze,
									onChange: (e) => Be(e.target.value)
								})]
							}),
							/* @__PURE__ */ (0, A.jsxs)("label", {
								className: "capability-field",
								children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "本机回调地址" }), /* @__PURE__ */ (0, A.jsx)("input", {
									value: Ve,
									onChange: (e) => He(e.target.value)
								})]
							}),
							/* @__PURE__ */ (0, A.jsxs)("label", {
								className: "capability-field",
								children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "资源地址（可选）" }), /* @__PURE__ */ (0, A.jsx)("input", {
									value: Ue,
									onChange: (e) => We(e.target.value)
								})]
							}),
							/* @__PURE__ */ (0, A.jsxs)("label", {
								className: "capability-field",
								children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "客户端元数据 URL（可选）" }), /* @__PURE__ */ (0, A.jsx)("input", {
									value: Ge,
									onChange: (e) => Ye(e.target.value)
								})]
							})
						] })
					] }),
					/* @__PURE__ */ (0, A.jsxs)("details", {
						className: "capability-field capability-field--wide mcp-advanced",
						children: [/* @__PURE__ */ (0, A.jsxs)("summary", { children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "更多连接选项" }), /* @__PURE__ */ (0, A.jsx)(O, {
							size: 15,
							"aria-hidden": "true"
						})] }), /* @__PURE__ */ (0, A.jsxs)("div", {
							className: "mcp-advanced-fields",
							children: [
								/* @__PURE__ */ (0, A.jsxs)("label", {
									className: "capability-field",
									children: [/* @__PURE__ */ (0, A.jsxs)("span", { children: [C === "stdio" ? "普通环境变量" : "普通 Header", "（JSON 对象，不填写密钥）"] }), /* @__PURE__ */ (0, A.jsx)("textarea", {
										value: C === "stdio" ? oe : k,
										onChange: (e) => C === "stdio" ? se(e.target.value) : ce(e.target.value)
									})]
								}),
								C !== "stdio" && /* @__PURE__ */ (0, A.jsxs)("label", {
									className: "capability-check",
									children: [/* @__PURE__ */ (0, A.jsx)("input", {
										type: "checkbox",
										checked: et,
										onChange: (e) => tt(e.target.checked)
									}), "允许本机 HTTP 测试地址"]
								}),
								!c && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
									C !== "stdio" && /* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "连接网络范围" }), /* @__PURE__ */ (0, A.jsxs)("select", {
											value: nt,
											onChange: (e) => rt(e.target.value),
											children: [/* @__PURE__ */ (0, A.jsx)("option", {
												value: "PUBLIC_ONLY",
												children: "仅公开网络（与明确允许的本机地址）"
											}), /* @__PURE__ */ (0, A.jsx)("option", {
												value: "ALLOW_PRIVATE",
												children: "也允许内网地址"
											})]
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("div", {
										className: "mcp-option-group",
										children: [
											C === "streamable_http" && /* @__PURE__ */ (0, A.jsxs)("label", {
												className: "capability-check",
												children: [/* @__PURE__ */ (0, A.jsx)("input", {
													type: "checkbox",
													checked: it,
													onChange: (e) => at(e.target.checked)
												}), "服务明确支持无状态请求"]
											}),
											/* @__PURE__ */ (0, A.jsxs)("label", {
												className: "capability-check",
												children: [/* @__PURE__ */ (0, A.jsx)("input", {
													type: "checkbox",
													checked: !!ft.required,
													onChange: (e) => ht("required", e.target.checked)
												}), "将此服务标记为必需"]
											}),
											/* @__PURE__ */ (0, A.jsxs)("label", {
												className: "capability-check",
												children: [/* @__PURE__ */ (0, A.jsx)("input", {
													type: "checkbox",
													checked: !!ft.supports_parallel_tool_calls,
													onChange: (e) => ht("supports_parallel_tool_calls", e.target.checked)
												}), "服务明确支持并行工具调用"]
											})
										]
									}),
									C === "streamable_http" && it && /* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "无状态请求并发数" }), /* @__PURE__ */ (0, A.jsx)("input", {
											type: "number",
											min: 1,
											max: 16,
											value: String(ft.stateless_http_max_in_flight ?? 1),
											onChange: (e) => ht("stateless_http_max_in_flight", Number(e.target.value))
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "单次工具超时（毫秒）" }), /* @__PURE__ */ (0, A.jsx)("input", {
											type: "number",
											min: 1e3,
											max: 6e5,
											value: String(ft.default_tool_timeout_ms ?? 6e5),
											onChange: (e) => ht("default_tool_timeout_ms", Number(e.target.value))
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-check",
										children: [/* @__PURE__ */ (0, A.jsx)("input", {
											type: "checkbox",
											checked: ft.catalog_refresh_interval_ms !== "DISABLED",
											onChange: (e) => ht("catalog_refresh_interval_ms", e.target.checked ? 3e5 : "DISABLED")
										}), "定期刷新服务目录"]
									}),
									ft.catalog_refresh_interval_ms !== "DISABLED" && /* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "目录刷新间隔（毫秒）" }), /* @__PURE__ */ (0, A.jsx)("input", {
											type: "number",
											min: 3e4,
											max: 864e5,
											value: String(ft.catalog_refresh_interval_ms ?? 3e5),
											onChange: (e) => ht("catalog_refresh_interval_ms", Number(e.target.value))
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-check",
										children: [/* @__PURE__ */ (0, A.jsx)("input", {
											type: "checkbox",
											checked: pt.include_tool_names != null,
											onChange: (e) => gt("include_tool_names", e.target.checked ? [] : null)
										}), "只提供指定工具"]
									}),
									pt.include_tool_names != null && /* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "提供的工具名（每行一个）" }), /* @__PURE__ */ (0, A.jsx)("textarea", {
											value: pt.include_tool_names.join("\n"),
											onChange: (e) => gt("include_tool_names", e.target.value.split("\n").filter(Boolean))
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "隐藏的工具名（每行一个）" }), /* @__PURE__ */ (0, A.jsx)("textarea", {
											value: (pt.exclude_tool_names ?? []).join("\n"),
											onChange: (e) => gt("exclude_tool_names", e.target.value.split("\n").filter(Boolean))
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "遇到无效工具定义" }), /* @__PURE__ */ (0, A.jsxs)("select", {
											value: String(pt.invalid_tool_policy ?? "FAIL_SERVER"),
											onChange: (e) => gt("invalid_tool_policy", e.target.value),
											children: [/* @__PURE__ */ (0, A.jsx)("option", {
												value: "FAIL_SERVER",
												children: "报告服务目录不可用"
											}), /* @__PURE__ */ (0, A.jsx)("option", {
												value: "OMIT_INVALID",
												children: "略过无效工具，保留其他工具"
											})]
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "默认工具作用" }), /* @__PURE__ */ (0, A.jsxs)("select", {
											value: String(mt.default_effect ?? "AUTO"),
											onChange: (e) => ht("effect_policy", {
												...mt,
												default_effect: e.target.value
											}),
											children: [
												/* @__PURE__ */ (0, A.jsx)("option", {
													value: "AUTO",
													children: "使用服务声明"
												}),
												/* @__PURE__ */ (0, A.jsx)("option", {
													value: "READ_ONLY",
													children: "只读"
												}),
												/* @__PURE__ */ (0, A.jsx)("option", {
													value: "EXTERNAL_EFFECT",
													children: "会改变外部状态"
												})
											]
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "单独设置工具超时（JSON：工具名 → 毫秒）" }), /* @__PURE__ */ (0, A.jsx)("textarea", {
											value: ct ?? qe(ft.per_tool_timeout_ms),
											onChange: (e) => lt(e.target.value)
										})]
									}),
									/* @__PURE__ */ (0, A.jsxs)("label", {
										className: "capability-field",
										children: [
											/* @__PURE__ */ (0, A.jsx)("span", { children: "单独设置工具作用（JSON）" }),
											/* @__PURE__ */ (0, A.jsx)("textarea", {
												value: ut ?? qe(mt.tool_effect_overrides),
												onChange: (e) => dt(e.target.value)
											}),
											/* @__PURE__ */ (0, A.jsx)("small", { children: "工具名对应 READ_ONLY（只读）或 EXTERNAL_EFFECT（改变外部状态）。" })
										]
									})
								] })
							]
						})]
					}),
					!c && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [/* @__PURE__ */ (0, A.jsxs)("label", {
						className: "capability-check capability-field--wide",
						children: [/* @__PURE__ */ (0, A.jsx)("input", {
							type: "checkbox",
							checked: Xe,
							onChange: (e) => Ze(e.target.checked)
						}), "启用此服务"]
					}), /* @__PURE__ */ (0, A.jsxs)("label", {
						className: "capability-check capability-field--wide",
						children: [/* @__PURE__ */ (0, A.jsx)("input", {
							type: "checkbox",
							checked: Qe,
							onChange: (e) => $e(e.target.checked)
						}), "也向子任务提供"]
					})] }),
					Tt && Ot && /* @__PURE__ */ (0, A.jsxs)("label", {
						className: "capability-check capability-field--wide",
						children: [/* @__PURE__ */ (0, A.jsx)("input", {
							type: "checkbox",
							checked: _t,
							onChange: (e) => vt(e.target.checked)
						}), "我确认将保留的凭据用于新的连接目标。"]
					}),
					xt && /* @__PURE__ */ (0, A.jsx)("p", {
						role: "alert",
						className: "capability-field--wide",
						children: xt
					}),
					Ct && /* @__PURE__ */ (0, A.jsx)("p", {
						role: "status",
						className: "capability-field--wide",
						children: Ct
					}),
					v.type === "oauth" && i && /* @__PURE__ */ (0, A.jsxs)("div", {
						className: "capability-field--wide",
						children: [/* @__PURE__ */ (0, A.jsx)("small", { children: "以下操作使用已保存的连接，不使用当前未保存的修改。" }), /* @__PURE__ */ (0, A.jsx)("div", {
							className: "capability-detail-actions",
							children: [
								"login",
								"status",
								"cancel",
								"logout"
							].map((e) => /* @__PURE__ */ (0, A.jsx)("button", {
								className: "secondary-action",
								disabled: yt,
								onClick: () => void (async () => {
									bt(!0);
									try {
										await i(e);
									} finally {
										bt(!1);
									}
								})(),
								children: {
									login: "登录授权",
									status: "查看授权状态",
									cancel: "取消登录",
									logout: "清除本机授权"
								}[e]
							}, e))
						})]
					})
				]
			}),
			/* @__PURE__ */ (0, A.jsxs)("footer", { children: [
				/* @__PURE__ */ (0, A.jsx)("button", {
					className: "secondary-action",
					disabled: yt,
					onClick: t,
					children: "取消"
				}),
				r && /* @__PURE__ */ (0, A.jsxs)("button", {
					className: "secondary-action",
					disabled: yt || !y.trim() || Tt && Ot && !_t,
					onClick: () => void kt(!0),
					children: [/* @__PURE__ */ (0, A.jsx)(Ce, { size: 14 }), "测试连接"]
				}),
				/* @__PURE__ */ (0, A.jsxs)("button", {
					className: "capability-primary",
					disabled: yt || !y.trim() || !(C === "stdio" ? D : E) || Tt && Ot && !_t,
					onClick: () => void kt(),
					children: [yt ? /* @__PURE__ */ (0, A.jsx)(ve, { size: 14 }) : /* @__PURE__ */ (0, A.jsx)(Ee, { size: 14 }), "保存连接"]
				})
			] })
		] })]
	});
}
//#endregion
//#region frontend/components/plugin-connection-editor.tsx
var Xe = (e) => e && typeof e == "object" && !Array.isArray(e) ? e : {};
function Ze({ plugin: e, connection: t, onClose: n, onSave: r, onAuthorization: i }) {
	let a = Xe(t.config.transport), o = {
		id: t.serverId,
		name: `${e.name} · ${t.serverId}`,
		enabled: e.enabled,
		status: e.enabled ? "configured" : "disabled",
		required: !1,
		availableToSubagents: !0,
		toolCount: 0,
		resourceCount: 0,
		resourceTemplateCount: 0,
		promptCount: 0,
		instructions: "",
		hasFailure: !1,
		transport: {
			kind: a.type === "stdio" ? "stdio" : "http",
			summary: String(a.command ?? a.endpoint ?? "")
		},
		tools: [],
		config: t.config,
		currentIdentity: e.packageInstallId
	};
	return /* @__PURE__ */ (0, A.jsx)(Ye, {
		server: o,
		credentialOwner: t.credentialOwner,
		connectionInputs: t.connectionInputs,
		packageDefinition: t.defaults,
		onClose: n,
		onSave: (e) => {
			let n = Xe(e.config.transport), i = Xe(t.defaults.transport), a = t.overlay ?? {}, o = (e, t, n) => Object.fromEntries(Object.entries(Xe(e)).filter(([e, r]) => r !== Xe(t)[e] || e in Xe(n)));
			return r({
				overlay: {
					local_server_id: t.serverId,
					transport_kind: n.type,
					endpoint: n.type === "stdio" ? null : n.endpoint !== i.endpoint || a.endpoint != null ? n.endpoint : null,
					public_headers: o(e.config.public_headers, t.defaults.public_headers, a.public_headers),
					environment: o(n.env, i.env, a.environment),
					secret_environment: Xe(n.secret_env),
					auth: e.config.auth
				},
				secretChanges: e.secretChanges,
				retainCredentialsConfirmed: e.retainCredentialsConfirmed
			});
		},
		onAuthorization: i,
		onRestoreDefaults: t.overlay ? () => r({
			overlay: null,
			secretChanges: []
		}) : void 0
	});
}
//#endregion
//#region frontend/components/capability-interaction-editor.tsx
var Qe = (e) => e && typeof e == "object" && !Array.isArray(e) ? e : {}, $e = {
	ADD_LOCAL_MCP: "添加 MCP 连接",
	UPDATE_LOCAL_MCP: "编辑 MCP 连接",
	REMOVE_LOCAL_MCP: "删除 MCP 连接",
	INSTALL_PLUGIN: "安装插件（暂不启用）",
	SET_PLUGIN_ENABLED: "更改插件启用状态",
	REMOVE_PLUGIN: "删除插件",
	CONFIGURE_PLUGIN_MCP_CONNECTION: "配置插件连接",
	AUTHORIZE_MCP: "在浏览器中登录",
	CLEAR_MCP_AUTHORIZATION: "退出 MCP 登录"
};
function et({ form: e, onResolve: t }) {
	let [n, r] = (0, w.useState)(!1), i = (0, w.useRef)(!1), [a, o] = (0, w.useState)(!1), s = String(e.action), c = Qe(e.prefill), l = Qe(e.plugin), u = async (e) => {
		if (i.current) return !1;
		i.current = !0, r(!0);
		try {
			let n = await t({
				kind: "capability",
				decision: "SUBMIT",
				submission: e
			});
			return n || (i.current = !1, r(!1)), n;
		} catch {
			return i.current = !1, r(!1), !1;
		}
	}, d = () => {
		i.current || (i.current = !0, r(!0), t({
			kind: "capability",
			decision: "CANCEL"
		}).then((e) => {
			e || (i.current = !1, r(!1));
		}).catch(() => {
			i.current = !1, r(!1);
		}));
	};
	if (s === "ADD_LOCAL_MCP" || s === "UPDATE_LOCAL_MCP") {
		let t = Qe(c.config), n = Qe(t.transport);
		return /* @__PURE__ */ (0, A.jsx)(Ye, {
			server: {
				id: String(c.server_id),
				name: String(t.display_name ?? c.server_id),
				config: t,
				currentIdentity: String(Qe(e.expected_current).expected_identity ?? ""),
				enabled: t.enabled !== !1,
				status: "configured",
				required: !1,
				availableToSubagents: !!t.available_to_subagents,
				toolCount: 0,
				resourceCount: 0,
				resourceTemplateCount: 0,
				promptCount: 0,
				instructions: "",
				hasFailure: !1,
				tools: [],
				transport: {
					kind: n.type === "stdio" ? "stdio" : "http",
					summary: String(n.endpoint ?? n.command ?? "")
				}
			},
			credentialOwner: e.credential_owner,
			onClose: d,
			onSave: (e) => u({
				config: e.config,
				secret_changes: e.secretChanges,
				retain_credentials_confirmed: e.retainCredentialsConfirmed ?? !1
			})
		});
	}
	if (s === "CONFIGURE_PLUGIN_MCP_CONNECTION") {
		let t = Qe(e.connection);
		return /* @__PURE__ */ (0, A.jsx)(Ze, {
			plugin: {
				id: String(l.id),
				name: String(l.name),
				description: "",
				enabled: !!l.enabled,
				packageInstallId: String(l.package_install_id),
				packageRoot: "",
				skillCount: 0,
				mcpCount: 0,
				effectiveSkillNames: [],
				effectiveMcpServerIds: [],
				details: []
			},
			connection: {
				serverId: String(t.server_id),
				defaults: Qe(t.defaults),
				config: Qe(t.config),
				overlay: t.overlay == null ? null : Qe(t.overlay),
				connectionInputs: t.connection_inputs,
				credentialOwner: t.credential_owner
			},
			onClose: d,
			onSave: (e) => u({
				overlay: e.overlay,
				secret_changes: e.secretChanges,
				retain_credentials_confirmed: e.retainCredentialsConfirmed ?? !1
			})
		});
	}
	let f = s === "SET_PLUGIN_ENABLED" && c.enabled === !0;
	return /* @__PURE__ */ (0, A.jsxs)("div", {
		className: "capability-form-review",
		children: [
			/* @__PURE__ */ (0, A.jsxs)("p", { children: [
				$e[s] ?? "确认能力配置",
				" · ",
				e.scope === "WORKSPACE" ? "当前项目" : "本机所有会话"
			] }),
			/* @__PURE__ */ (0, A.jsx)("p", { children: String(c.plugin_id ?? c.server_id ?? c.source_path ?? "") }),
			f && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
				/* @__PURE__ */ (0, A.jsx)("p", { children: "启用后，下列能力将供后续工作使用。请确认你信任插件来源。" }),
				Array.isArray(l.skills) && l.skills.map((e, t) => /* @__PURE__ */ (0, A.jsxs)("p", { children: [
					"Skill · ",
					String(Qe(e).name),
					" — ",
					String(Qe(e).description)
				] }, `skill-${t}`)),
				Array.isArray(l.mcp) && l.mcp.map((e, t) => {
					let n = Qe(e), r = Qe(n.config), i = Qe(r.transport), a = Qe(r.auth);
					return /* @__PURE__ */ (0, A.jsxs)("div", { children: [
						/* @__PURE__ */ (0, A.jsxs)("p", { children: ["MCP · ", String(n.server_id)] }),
						/* @__PURE__ */ (0, A.jsx)("code", { children: String(i.endpoint ?? i.command ?? "") }),
						i.type === "stdio" && /* @__PURE__ */ (0, A.jsxs)("p", { children: [
							"参数：",
							JSON.stringify(i.args ?? []),
							" · 目录：",
							String(i.cwd ?? "")
						] }),
						/* @__PURE__ */ (0, A.jsxs)("p", { children: ["认证：", Ge[String(a.type)] ?? "未指定"] }),
						Array.isArray(n.credentials) && n.credentials.map((e, t) => {
							let n = Qe(e);
							return /* @__PURE__ */ (0, A.jsxs)("p", { children: [
								String(n.name),
								"：",
								n.present ? "已配置" : "需要配置"
							] }, t);
						})
					] }, `mcp-${t}`);
				}),
				Array.isArray(l.hooks) && l.hooks.map((e, t) => /* @__PURE__ */ (0, A.jsxs)("p", { children: [
					"Hook · ",
					String(Qe(e).event),
					" · ",
					/* @__PURE__ */ (0, A.jsx)("code", { children: String(Qe(e).command) })
				] }, `hook-${t}`)),
				/* @__PURE__ */ (0, A.jsxs)("label", {
					className: "capability-choice",
					children: [/* @__PURE__ */ (0, A.jsx)("input", {
						type: "checkbox",
						checked: a,
						onChange: (e) => o(e.target.checked)
					}), /* @__PURE__ */ (0, A.jsx)("span", { children: "我已审阅并允许启用此插件" })]
				})
			] }),
			s === "AUTHORIZE_MCP" && /* @__PURE__ */ (0, A.jsx)("p", { children: "确认后将打开服务商登录页。无需把登录信息或密钥发给模型。" }),
			(s === "REMOVE_LOCAL_MCP" || s === "REMOVE_PLUGIN") && /* @__PURE__ */ (0, A.jsx)("p", { children: "将移除该能力及其连接凭据；已有对话记录不会删除。" }),
			/* @__PURE__ */ (0, A.jsxs)("div", {
				className: "interaction-actions",
				children: [/* @__PURE__ */ (0, A.jsx)("button", {
					disabled: n,
					onClick: d,
					children: "取消"
				}), /* @__PURE__ */ (0, A.jsx)("button", {
					className: "is-primary",
					disabled: n || f && !a,
					onClick: () => void u(f ? { enable_review_accepted: !0 } : {}),
					children: "确认并继续"
				})]
			})
		]
	});
}
//#endregion
//#region frontend/lib/pulsara-types.ts
var tt = {
	"accept-edits": "接受编辑",
	"read-only": "只读",
	"ask-permissions": "每次询问",
	"bypass-permissions": "完全访问"
}, nt = [
	"read-only",
	"ask-permissions",
	"accept-edits",
	"bypass-permissions"
], rt = (function(e) {
	if (e == null) return ct;
	if (typeof e == "function") return st(e);
	if (typeof e == "object") return Array.isArray(e) ? it(e) : at(e);
	if (typeof e == "string") return ot(e);
	throw Error("Expected function, string, or object as test");
});
function it(e) {
	let t = [], n = -1;
	for (; ++n < e.length;) t[n] = rt(e[n]);
	return st(r);
	function r(...e) {
		let n = -1;
		for (; ++n < t.length;) if (t[n].apply(this, e)) return !0;
		return !1;
	}
}
function at(e) {
	let t = e;
	return st(n);
	function n(n) {
		let r = n, i;
		for (i in e) if (r[i] !== t[i]) return !1;
		return !0;
	}
}
function ot(e) {
	return st(t);
	function t(t) {
		return t && t.type === e;
	}
}
function st(e) {
	return t;
	function t(t, n, r) {
		return !!(lt(t) && e.call(this, t, typeof n == "number" ? n : void 0, r || void 0));
	}
}
function ct() {
	return !0;
}
function lt(e) {
	return typeof e == "object" && !!e && "type" in e;
}
//#endregion
//#region frontend/node_modules/unist-util-find-after/lib/index.js
var ut = (function(e, t, n) {
	let r = rt(n);
	if (!e || !e.type || !e.children) throw Error("Expected parent node");
	if (typeof t == "number") {
		if (t < 0 || t === Infinity) throw Error("Expected positive finite number as index");
	} else if (t = e.children.indexOf(t), t < 0) throw Error("Expected child node or index");
	for (; ++t < e.children.length;) if (r(e.children[t], t, e)) return e.children[t];
}), dt = (function(e) {
	if (e == null) return ht;
	if (typeof e == "string") return pt(e);
	if (typeof e == "object") return ft(e);
	if (typeof e == "function") return mt(e);
	throw Error("Expected function, string, or array as `test`");
});
function ft(e) {
	let t = [], n = -1;
	for (; ++n < e.length;) t[n] = dt(e[n]);
	return mt(r);
	function r(...e) {
		let n = -1;
		for (; ++n < t.length;) if (t[n].apply(this, e)) return !0;
		return !1;
	}
}
function pt(e) {
	return mt(t);
	function t(t) {
		return t.tagName === e;
	}
}
function mt(e) {
	return t;
	function t(t, n, r) {
		return !!(gt(t) && e.call(this, t, typeof n == "number" ? n : void 0, r || void 0));
	}
}
function ht(e) {
	return !!(e && typeof e == "object" && "type" in e && e.type === "element" && "tagName" in e && typeof e.tagName == "string");
}
function gt(e) {
	return typeof e == "object" && !!e && "type" in e && "tagName" in e;
}
//#endregion
//#region frontend/node_modules/hast-util-to-text/lib/index.js
var _t = /\n/g, vt = /[\t ]+/g, yt = dt("br"), bt = dt(Nt), xt = dt("p"), St = dt("tr"), Ct = dt([
	"datalist",
	"head",
	"noembed",
	"noframes",
	"noscript",
	"rp",
	"script",
	"style",
	"template",
	"title",
	Mt,
	Pt
]), wt = dt(/* @__PURE__ */ "address.article.aside.blockquote.body.caption.center.dd.dialog.dir.dl.dt.div.figure.figcaption.footer.form,.h1.h2.h3.h4.h5.h6.header.hgroup.hr.html.legend.li.listing.main.menu.nav.ol.p.plaintext.pre.section.ul.xmp".split("."));
function Tt(e, t) {
	let n = t || {}, r = "children" in e ? e.children : [], i = wt(e), a = jt(e, {
		whitespace: n.whitespace || "normal",
		breakBefore: !1,
		breakAfter: !1
	}), o = [];
	(e.type === "text" || e.type === "comment") && o.push(...Ot(e, {
		whitespace: a,
		breakBefore: !0,
		breakAfter: !0
	}));
	let s = -1;
	for (; ++s < r.length;) o.push(...Et(r[s], e, {
		whitespace: a,
		breakBefore: s ? void 0 : i,
		breakAfter: s < r.length - 1 ? yt(r[s + 1]) : i
	}));
	let c = [], l;
	for (s = -1; ++s < o.length;) {
		let e = o[s];
		typeof e == "number" ? l !== void 0 && e > l && (l = e) : e && (l !== void 0 && l > -1 && c.push("\n".repeat(l) || " "), l = -1, c.push(e));
	}
	return c.join("");
}
function Et(e, t, n) {
	return e.type === "element" ? Dt(e, t, n) : e.type === "text" ? n.whitespace === "normal" ? Ot(e, n) : kt(e) : [];
}
function Dt(e, t, n) {
	let r = jt(e, n), i = e.children || [], a = -1, o = [];
	if (Ct(e)) return o;
	let s, c;
	for (yt(e) || St(e) && ut(t, e, St) ? c = "\n" : xt(e) ? (s = 2, c = 2) : wt(e) && (s = 1, c = 1); ++a < i.length;) o = o.concat(Et(i[a], e, {
		whitespace: r,
		breakBefore: a ? void 0 : s,
		breakAfter: a < i.length - 1 ? yt(i[a + 1]) : c
	}));
	return bt(e) && ut(t, e, bt) && o.push("	"), s && o.unshift(s), c && o.push(c), o;
}
function Ot(e, t) {
	let n = String(e.value), r = [], i = [], a = 0;
	for (; a <= n.length;) {
		_t.lastIndex = a;
		let e = _t.exec(n), i = e && "index" in e ? e.index : n.length;
		r.push(At(n.slice(a, i).replace(/[\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/g, ""), a === 0 ? t.breakBefore : !0, i === n.length ? t.breakAfter : !0)), a = i + 1;
	}
	let o = -1, s;
	for (; ++o < r.length;) r[o].charCodeAt(r[o].length - 1) === 8203 || o < r.length - 1 && r[o + 1].charCodeAt(0) === 8203 ? (i.push(r[o]), s = void 0) : r[o] ? (typeof s == "number" && i.push(s), i.push(r[o]), s = 0) : (o === 0 || o === r.length - 1) && i.push(0);
	return i;
}
function kt(e) {
	return [String(e.value)];
}
function At(e, t, n) {
	let r = [], i = 0, a;
	for (; i < e.length;) {
		vt.lastIndex = i;
		let n = vt.exec(e);
		a = n ? n.index : e.length, !i && !a && n && !t && r.push(""), i !== a && r.push(e.slice(i, a)), i = n ? a + n[0].length : a;
	}
	return i !== a && !n && r.push(""), r.join(" ");
}
function jt(e, t) {
	if (e.type === "element") {
		let n = e.properties || {};
		switch (e.tagName) {
			case "listing":
			case "plaintext":
			case "xmp": return "pre";
			case "nobr": return "nowrap";
			case "pre": return n.wrap ? "pre-wrap" : "pre";
			case "td":
			case "th": return n.noWrap ? "nowrap" : t.whitespace;
			case "textarea": return "pre-wrap";
			default:
		}
	}
	return t.whitespace;
}
function Mt(e) {
	return !!(e.properties || {}).hidden;
}
function Nt(e) {
	return e.tagName === "td" || e.tagName === "th";
}
function Pt(e) {
	return e.tagName === "dialog" && !(e.properties || {}).open;
}
//#endregion
//#region frontend/node_modules/property-information/lib/util/schema.js
var Ft = class {
	constructor(e, t, n) {
		this.normal = t, this.property = e, n && (this.space = n);
	}
};
Ft.prototype.normal = {}, Ft.prototype.property = {}, Ft.prototype.space = void 0;
//#endregion
//#region frontend/node_modules/property-information/lib/util/merge.js
function It(e, t) {
	let n = {}, r = {};
	for (let t of e) Object.assign(n, t.property), Object.assign(r, t.normal);
	return new Ft(n, r, t);
}
//#endregion
//#region frontend/node_modules/property-information/lib/normalize.js
function Lt(e) {
	return e.toLowerCase();
}
//#endregion
//#region frontend/node_modules/property-information/lib/util/info.js
var Rt = class {
	constructor(e, t) {
		this.attribute = t, this.property = e;
	}
};
Rt.prototype.attribute = "", Rt.prototype.booleanish = !1, Rt.prototype.boolean = !1, Rt.prototype.commaOrSpaceSeparated = !1, Rt.prototype.commaSeparated = !1, Rt.prototype.defined = !1, Rt.prototype.mustUseProperty = !1, Rt.prototype.number = !1, Rt.prototype.overloadedBoolean = !1, Rt.prototype.property = "", Rt.prototype.spaceSeparated = !1, Rt.prototype.space = void 0;
//#endregion
//#region frontend/node_modules/property-information/lib/util/types.js
var zt = /* @__PURE__ */ s({
	boolean: () => j,
	booleanish: () => Vt,
	commaOrSpaceSeparated: () => Gt,
	commaSeparated: () => Wt,
	number: () => M,
	overloadedBoolean: () => Ht,
	spaceSeparated: () => Ut
}), Bt = 0, j = Kt(), Vt = Kt(), Ht = Kt(), M = Kt(), Ut = Kt(), Wt = Kt(), Gt = Kt();
function Kt() {
	return 2 ** ++Bt;
}
//#endregion
//#region frontend/node_modules/property-information/lib/util/defined-info.js
var qt = Object.keys(zt), Jt = class extends Rt {
	constructor(e, t, n, r) {
		let i = -1;
		if (super(e, t), Yt(this, "space", r), typeof n == "number") for (; ++i < qt.length;) {
			let e = qt[i];
			Yt(this, qt[i], (n & zt[e]) === zt[e]);
		}
	}
};
Jt.prototype.defined = !0;
function Yt(e, t, n) {
	n && (e[t] = n);
}
//#endregion
//#region frontend/node_modules/property-information/lib/util/create.js
function Xt(e) {
	let t = {}, n = {};
	for (let [r, i] of Object.entries(e.properties)) {
		let a = new Jt(r, e.transform(e.attributes || {}, r), i, e.space);
		e.mustUseProperty && e.mustUseProperty.includes(r) && (a.mustUseProperty = !0), t[r] = a, n[Lt(r)] = r, n[Lt(a.attribute)] = r;
	}
	return new Ft(t, n, e.space);
}
//#endregion
//#region frontend/node_modules/property-information/lib/aria.js
var Zt = Xt({
	properties: {
		ariaActiveDescendant: null,
		ariaAtomic: Vt,
		ariaAutoComplete: null,
		ariaBusy: Vt,
		ariaChecked: Vt,
		ariaColCount: M,
		ariaColIndex: M,
		ariaColSpan: M,
		ariaControls: Ut,
		ariaCurrent: null,
		ariaDescribedBy: Ut,
		ariaDetails: null,
		ariaDisabled: Vt,
		ariaDropEffect: Ut,
		ariaErrorMessage: null,
		ariaExpanded: Vt,
		ariaFlowTo: Ut,
		ariaGrabbed: Vt,
		ariaHasPopup: null,
		ariaHidden: Vt,
		ariaInvalid: null,
		ariaKeyShortcuts: null,
		ariaLabel: null,
		ariaLabelledBy: Ut,
		ariaLevel: M,
		ariaLive: null,
		ariaModal: Vt,
		ariaMultiLine: Vt,
		ariaMultiSelectable: Vt,
		ariaOrientation: null,
		ariaOwns: Ut,
		ariaPlaceholder: null,
		ariaPosInSet: M,
		ariaPressed: Vt,
		ariaReadOnly: Vt,
		ariaRelevant: null,
		ariaRequired: Vt,
		ariaRoleDescription: Ut,
		ariaRowCount: M,
		ariaRowIndex: M,
		ariaRowSpan: M,
		ariaSelected: Vt,
		ariaSetSize: M,
		ariaSort: null,
		ariaValueMax: M,
		ariaValueMin: M,
		ariaValueNow: M,
		ariaValueText: null,
		role: null
	},
	transform(e, t) {
		return t === "role" ? t : "aria-" + t.slice(4).toLowerCase();
	}
});
//#endregion
//#region frontend/node_modules/property-information/lib/util/case-sensitive-transform.js
function Qt(e, t) {
	return t in e ? e[t] : t;
}
//#endregion
//#region frontend/node_modules/property-information/lib/util/case-insensitive-transform.js
function $t(e, t) {
	return Qt(e, t.toLowerCase());
}
//#endregion
//#region frontend/node_modules/property-information/lib/html.js
var en = Xt({
	attributes: {
		acceptcharset: "accept-charset",
		classname: "class",
		htmlfor: "for",
		httpequiv: "http-equiv"
	},
	mustUseProperty: [
		"checked",
		"multiple",
		"muted",
		"selected"
	],
	properties: {
		abbr: null,
		accept: Wt,
		acceptCharset: Ut,
		accessKey: Ut,
		action: null,
		allow: null,
		allowFullScreen: j,
		allowPaymentRequest: j,
		allowUserMedia: j,
		alpha: j,
		alt: null,
		as: null,
		async: j,
		autoCapitalize: null,
		autoComplete: Ut,
		autoFocus: j,
		autoPlay: j,
		blocking: Ut,
		capture: null,
		charSet: null,
		checked: j,
		cite: null,
		className: Ut,
		closedBy: null,
		colorSpace: null,
		cols: M,
		colSpan: M,
		command: null,
		commandFor: null,
		content: null,
		contentEditable: Vt,
		controls: j,
		controlsList: Ut,
		coords: M | Wt,
		crossOrigin: null,
		data: null,
		dateTime: null,
		decoding: null,
		default: j,
		defer: j,
		dir: null,
		dirName: null,
		disabled: j,
		download: Ht,
		draggable: Vt,
		encType: null,
		enterKeyHint: null,
		fetchPriority: null,
		form: null,
		formAction: null,
		formEncType: null,
		formMethod: null,
		formNoValidate: j,
		formTarget: null,
		headers: Ut,
		height: M,
		hidden: Ht,
		high: M,
		href: null,
		hrefLang: null,
		htmlFor: Ut,
		httpEquiv: Ut,
		id: null,
		imageSizes: null,
		imageSrcSet: null,
		inert: j,
		inputMode: null,
		integrity: null,
		is: null,
		isMap: j,
		itemId: null,
		itemProp: Ut,
		itemRef: Ut,
		itemScope: j,
		itemType: Ut,
		kind: null,
		label: null,
		lang: null,
		language: null,
		list: null,
		loading: null,
		loop: j,
		low: M,
		manifest: null,
		max: null,
		maxLength: M,
		media: null,
		method: null,
		min: null,
		minLength: M,
		multiple: j,
		muted: j,
		name: null,
		nonce: null,
		noModule: j,
		noValidate: j,
		onAbort: null,
		onAfterPrint: null,
		onAuxClick: null,
		onBeforeMatch: null,
		onBeforePrint: null,
		onBeforeToggle: null,
		onBeforeUnload: null,
		onBlur: null,
		onCancel: null,
		onCanPlay: null,
		onCanPlayThrough: null,
		onChange: null,
		onClick: null,
		onClose: null,
		onContextLost: null,
		onContextMenu: null,
		onContextRestored: null,
		onCopy: null,
		onCueChange: null,
		onCut: null,
		onDblClick: null,
		onDrag: null,
		onDragEnd: null,
		onDragEnter: null,
		onDragExit: null,
		onDragLeave: null,
		onDragOver: null,
		onDragStart: null,
		onDrop: null,
		onDurationChange: null,
		onEmptied: null,
		onEnded: null,
		onError: null,
		onFocus: null,
		onFormData: null,
		onHashChange: null,
		onInput: null,
		onInvalid: null,
		onKeyDown: null,
		onKeyPress: null,
		onKeyUp: null,
		onLanguageChange: null,
		onLoad: null,
		onLoadedData: null,
		onLoadedMetadata: null,
		onLoadEnd: null,
		onLoadStart: null,
		onMessage: null,
		onMessageError: null,
		onMouseDown: null,
		onMouseEnter: null,
		onMouseLeave: null,
		onMouseMove: null,
		onMouseOut: null,
		onMouseOver: null,
		onMouseUp: null,
		onOffline: null,
		onOnline: null,
		onPageHide: null,
		onPageShow: null,
		onPaste: null,
		onPause: null,
		onPlay: null,
		onPlaying: null,
		onPopState: null,
		onProgress: null,
		onRateChange: null,
		onRejectionHandled: null,
		onReset: null,
		onResize: null,
		onScroll: null,
		onScrollEnd: null,
		onSecurityPolicyViolation: null,
		onSeeked: null,
		onSeeking: null,
		onSelect: null,
		onSlotChange: null,
		onStalled: null,
		onStorage: null,
		onSubmit: null,
		onSuspend: null,
		onTimeUpdate: null,
		onToggle: null,
		onUnhandledRejection: null,
		onUnload: null,
		onVolumeChange: null,
		onWaiting: null,
		onWheel: null,
		open: j,
		optimum: M,
		pattern: null,
		ping: Ut,
		placeholder: null,
		playsInline: j,
		popover: null,
		popoverTarget: null,
		popoverTargetAction: null,
		poster: null,
		preload: null,
		readOnly: j,
		referrerPolicy: null,
		rel: Ut,
		required: j,
		reversed: j,
		rows: M,
		rowSpan: M,
		sandbox: Ut,
		scope: null,
		scoped: j,
		seamless: j,
		selected: j,
		shadowRootClonable: j,
		shadowRootCustomElementRegistry: j,
		shadowRootDelegatesFocus: j,
		shadowRootMode: null,
		shadowRootSerializable: j,
		shape: null,
		size: M,
		sizes: null,
		slot: null,
		span: M,
		spellCheck: Vt,
		src: null,
		srcDoc: null,
		srcLang: null,
		srcSet: null,
		start: M,
		step: null,
		style: null,
		tabIndex: M,
		target: null,
		title: null,
		translate: null,
		type: null,
		typeMustMatch: j,
		useMap: null,
		value: Vt,
		width: M,
		wrap: null,
		writingSuggestions: null,
		align: null,
		aLink: null,
		archive: Ut,
		axis: null,
		background: null,
		bgColor: null,
		border: M,
		borderColor: null,
		bottomMargin: M,
		cellPadding: null,
		cellSpacing: null,
		char: null,
		charOff: null,
		classId: null,
		clear: null,
		code: null,
		codeBase: null,
		codeType: null,
		color: null,
		compact: j,
		declare: j,
		event: null,
		face: null,
		frame: null,
		frameBorder: null,
		hSpace: M,
		leftMargin: M,
		link: null,
		longDesc: null,
		lowSrc: null,
		marginHeight: M,
		marginWidth: M,
		noResize: j,
		noHref: j,
		noShade: j,
		noWrap: j,
		object: null,
		profile: null,
		prompt: null,
		rev: null,
		rightMargin: M,
		rules: null,
		scheme: null,
		scrolling: Vt,
		standby: null,
		summary: null,
		text: null,
		topMargin: M,
		valueType: null,
		version: null,
		vAlign: null,
		vLink: null,
		vSpace: M,
		allowTransparency: null,
		autoCorrect: null,
		autoSave: null,
		credentialless: j,
		disablePictureInPicture: j,
		disableRemotePlayback: j,
		exportParts: Wt,
		part: Ut,
		prefix: null,
		property: null,
		results: M,
		security: null,
		unselectable: null
	},
	space: "html",
	transform: $t
}), tn = Xt({
	attributes: {
		accentHeight: "accent-height",
		alignmentBaseline: "alignment-baseline",
		arabicForm: "arabic-form",
		baselineShift: "baseline-shift",
		capHeight: "cap-height",
		className: "class",
		clipPath: "clip-path",
		clipRule: "clip-rule",
		colorInterpolation: "color-interpolation",
		colorInterpolationFilters: "color-interpolation-filters",
		colorProfile: "color-profile",
		colorRendering: "color-rendering",
		crossOrigin: "crossorigin",
		dataType: "datatype",
		dominantBaseline: "dominant-baseline",
		enableBackground: "enable-background",
		fillOpacity: "fill-opacity",
		fillRule: "fill-rule",
		floodColor: "flood-color",
		floodOpacity: "flood-opacity",
		fontFamily: "font-family",
		fontSize: "font-size",
		fontSizeAdjust: "font-size-adjust",
		fontStretch: "font-stretch",
		fontStyle: "font-style",
		fontVariant: "font-variant",
		fontWeight: "font-weight",
		glyphName: "glyph-name",
		glyphOrientationHorizontal: "glyph-orientation-horizontal",
		glyphOrientationVertical: "glyph-orientation-vertical",
		hrefLang: "hreflang",
		horizAdvX: "horiz-adv-x",
		horizOriginX: "horiz-origin-x",
		horizOriginY: "horiz-origin-y",
		imageRendering: "image-rendering",
		letterSpacing: "letter-spacing",
		lightingColor: "lighting-color",
		markerEnd: "marker-end",
		markerMid: "marker-mid",
		markerStart: "marker-start",
		maskType: "mask-type",
		navDown: "nav-down",
		navDownLeft: "nav-down-left",
		navDownRight: "nav-down-right",
		navLeft: "nav-left",
		navNext: "nav-next",
		navPrev: "nav-prev",
		navRight: "nav-right",
		navUp: "nav-up",
		navUpLeft: "nav-up-left",
		navUpRight: "nav-up-right",
		onAbort: "onabort",
		onActivate: "onactivate",
		onAfterPrint: "onafterprint",
		onBeforePrint: "onbeforeprint",
		onBegin: "onbegin",
		onCancel: "oncancel",
		onCanPlay: "oncanplay",
		onCanPlayThrough: "oncanplaythrough",
		onChange: "onchange",
		onClick: "onclick",
		onClose: "onclose",
		onCopy: "oncopy",
		onCueChange: "oncuechange",
		onCut: "oncut",
		onDblClick: "ondblclick",
		onDrag: "ondrag",
		onDragEnd: "ondragend",
		onDragEnter: "ondragenter",
		onDragExit: "ondragexit",
		onDragLeave: "ondragleave",
		onDragOver: "ondragover",
		onDragStart: "ondragstart",
		onDrop: "ondrop",
		onDurationChange: "ondurationchange",
		onEmptied: "onemptied",
		onEnd: "onend",
		onEnded: "onended",
		onError: "onerror",
		onFocus: "onfocus",
		onFocusIn: "onfocusin",
		onFocusOut: "onfocusout",
		onHashChange: "onhashchange",
		onInput: "oninput",
		onInvalid: "oninvalid",
		onKeyDown: "onkeydown",
		onKeyPress: "onkeypress",
		onKeyUp: "onkeyup",
		onLoad: "onload",
		onLoadedData: "onloadeddata",
		onLoadedMetadata: "onloadedmetadata",
		onLoadStart: "onloadstart",
		onMessage: "onmessage",
		onMouseDown: "onmousedown",
		onMouseEnter: "onmouseenter",
		onMouseLeave: "onmouseleave",
		onMouseMove: "onmousemove",
		onMouseOut: "onmouseout",
		onMouseOver: "onmouseover",
		onMouseUp: "onmouseup",
		onMouseWheel: "onmousewheel",
		onOffline: "onoffline",
		onOnline: "ononline",
		onPageHide: "onpagehide",
		onPageShow: "onpageshow",
		onPaste: "onpaste",
		onPause: "onpause",
		onPlay: "onplay",
		onPlaying: "onplaying",
		onPopState: "onpopstate",
		onProgress: "onprogress",
		onRateChange: "onratechange",
		onRepeat: "onrepeat",
		onReset: "onreset",
		onResize: "onresize",
		onScroll: "onscroll",
		onSeeked: "onseeked",
		onSeeking: "onseeking",
		onSelect: "onselect",
		onShow: "onshow",
		onStalled: "onstalled",
		onStorage: "onstorage",
		onSubmit: "onsubmit",
		onSuspend: "onsuspend",
		onTimeUpdate: "ontimeupdate",
		onToggle: "ontoggle",
		onUnload: "onunload",
		onVolumeChange: "onvolumechange",
		onWaiting: "onwaiting",
		onZoom: "onzoom",
		overlinePosition: "overline-position",
		overlineThickness: "overline-thickness",
		paintOrder: "paint-order",
		panose1: "panose-1",
		pointerEvents: "pointer-events",
		referrerPolicy: "referrerpolicy",
		renderingIntent: "rendering-intent",
		shapeRendering: "shape-rendering",
		stopColor: "stop-color",
		stopOpacity: "stop-opacity",
		strikethroughPosition: "strikethrough-position",
		strikethroughThickness: "strikethrough-thickness",
		strokeDashArray: "stroke-dasharray",
		strokeDashOffset: "stroke-dashoffset",
		strokeLineCap: "stroke-linecap",
		strokeLineJoin: "stroke-linejoin",
		strokeMiterLimit: "stroke-miterlimit",
		strokeOpacity: "stroke-opacity",
		strokeWidth: "stroke-width",
		tabIndex: "tabindex",
		textAnchor: "text-anchor",
		textDecoration: "text-decoration",
		textRendering: "text-rendering",
		transformOrigin: "transform-origin",
		typeOf: "typeof",
		underlinePosition: "underline-position",
		underlineThickness: "underline-thickness",
		unicodeBidi: "unicode-bidi",
		unicodeRange: "unicode-range",
		unitsPerEm: "units-per-em",
		vAlphabetic: "v-alphabetic",
		vHanging: "v-hanging",
		vIdeographic: "v-ideographic",
		vMathematical: "v-mathematical",
		vectorEffect: "vector-effect",
		vertAdvY: "vert-adv-y",
		vertOriginX: "vert-origin-x",
		vertOriginY: "vert-origin-y",
		wordSpacing: "word-spacing",
		writingMode: "writing-mode",
		xHeight: "x-height",
		playbackOrder: "playbackorder",
		timelineBegin: "timelinebegin"
	},
	properties: {
		about: Gt,
		accentHeight: M,
		accumulate: null,
		additive: null,
		alignmentBaseline: null,
		alphabetic: M,
		amplitude: M,
		arabicForm: null,
		ascent: M,
		attributeName: null,
		attributeType: null,
		azimuth: M,
		bandwidth: null,
		baselineShift: null,
		baseFrequency: null,
		baseProfile: null,
		bbox: null,
		begin: null,
		bias: M,
		by: null,
		calcMode: null,
		capHeight: M,
		className: Ut,
		clip: null,
		clipPath: null,
		clipPathUnits: null,
		clipRule: null,
		color: null,
		colorInterpolation: null,
		colorInterpolationFilters: null,
		colorProfile: null,
		colorRendering: null,
		content: null,
		contentScriptType: null,
		contentStyleType: null,
		crossOrigin: null,
		cursor: null,
		cx: null,
		cy: null,
		d: null,
		dataType: null,
		defaultAction: null,
		descent: M,
		diffuseConstant: M,
		direction: null,
		display: null,
		dur: null,
		divisor: M,
		dominantBaseline: null,
		download: j,
		dx: null,
		dy: null,
		edgeMode: null,
		editable: null,
		elevation: M,
		enableBackground: null,
		end: null,
		event: null,
		exponent: M,
		externalResourcesRequired: null,
		fill: null,
		fillOpacity: M,
		fillRule: null,
		filter: null,
		filterRes: null,
		filterUnits: null,
		floodColor: null,
		floodOpacity: null,
		focusable: null,
		focusHighlight: null,
		fontFamily: null,
		fontSize: null,
		fontSizeAdjust: null,
		fontStretch: null,
		fontStyle: null,
		fontVariant: null,
		fontWeight: null,
		format: null,
		fr: null,
		from: null,
		fx: null,
		fy: null,
		g1: Wt,
		g2: Wt,
		glyphName: Wt,
		glyphOrientationHorizontal: null,
		glyphOrientationVertical: null,
		glyphRef: null,
		gradientTransform: null,
		gradientUnits: null,
		handler: null,
		hanging: M,
		hatchContentUnits: null,
		hatchUnits: null,
		height: null,
		href: null,
		hrefLang: null,
		horizAdvX: M,
		horizOriginX: M,
		horizOriginY: M,
		id: null,
		ideographic: M,
		imageRendering: null,
		initialVisibility: null,
		in: null,
		in2: null,
		intercept: M,
		k: M,
		k1: M,
		k2: M,
		k3: M,
		k4: M,
		kernelMatrix: Gt,
		kernelUnitLength: null,
		keyPoints: null,
		keySplines: null,
		keyTimes: null,
		kerning: null,
		lang: null,
		lengthAdjust: null,
		letterSpacing: null,
		lightingColor: null,
		limitingConeAngle: M,
		local: null,
		markerEnd: null,
		markerMid: null,
		markerStart: null,
		markerHeight: null,
		markerUnits: null,
		markerWidth: null,
		mask: null,
		maskContentUnits: null,
		maskType: null,
		maskUnits: null,
		mathematical: null,
		max: null,
		media: null,
		mediaCharacterEncoding: null,
		mediaContentEncodings: null,
		mediaSize: M,
		mediaTime: null,
		method: null,
		min: null,
		mode: null,
		name: null,
		navDown: null,
		navDownLeft: null,
		navDownRight: null,
		navLeft: null,
		navNext: null,
		navPrev: null,
		navRight: null,
		navUp: null,
		navUpLeft: null,
		navUpRight: null,
		numOctaves: null,
		observer: null,
		offset: null,
		onAbort: null,
		onActivate: null,
		onAfterPrint: null,
		onBeforePrint: null,
		onBegin: null,
		onCancel: null,
		onCanPlay: null,
		onCanPlayThrough: null,
		onChange: null,
		onClick: null,
		onClose: null,
		onCopy: null,
		onCueChange: null,
		onCut: null,
		onDblClick: null,
		onDrag: null,
		onDragEnd: null,
		onDragEnter: null,
		onDragExit: null,
		onDragLeave: null,
		onDragOver: null,
		onDragStart: null,
		onDrop: null,
		onDurationChange: null,
		onEmptied: null,
		onEnd: null,
		onEnded: null,
		onError: null,
		onFocus: null,
		onFocusIn: null,
		onFocusOut: null,
		onHashChange: null,
		onInput: null,
		onInvalid: null,
		onKeyDown: null,
		onKeyPress: null,
		onKeyUp: null,
		onLoad: null,
		onLoadedData: null,
		onLoadedMetadata: null,
		onLoadStart: null,
		onMessage: null,
		onMouseDown: null,
		onMouseEnter: null,
		onMouseLeave: null,
		onMouseMove: null,
		onMouseOut: null,
		onMouseOver: null,
		onMouseUp: null,
		onMouseWheel: null,
		onOffline: null,
		onOnline: null,
		onPageHide: null,
		onPageShow: null,
		onPaste: null,
		onPause: null,
		onPlay: null,
		onPlaying: null,
		onPopState: null,
		onProgress: null,
		onRateChange: null,
		onRepeat: null,
		onReset: null,
		onResize: null,
		onScroll: null,
		onSeeked: null,
		onSeeking: null,
		onSelect: null,
		onShow: null,
		onStalled: null,
		onStorage: null,
		onSubmit: null,
		onSuspend: null,
		onTimeUpdate: null,
		onToggle: null,
		onUnload: null,
		onVolumeChange: null,
		onWaiting: null,
		onZoom: null,
		opacity: null,
		operator: null,
		order: null,
		orient: null,
		orientation: null,
		origin: null,
		overflow: null,
		overlay: null,
		overlinePosition: M,
		overlineThickness: M,
		paintOrder: null,
		panose1: null,
		path: null,
		pathLength: M,
		patternContentUnits: null,
		patternTransform: null,
		patternUnits: null,
		phase: null,
		ping: Ut,
		pitch: null,
		playbackOrder: null,
		pointerEvents: null,
		points: null,
		pointsAtX: M,
		pointsAtY: M,
		pointsAtZ: M,
		preserveAlpha: null,
		preserveAspectRatio: null,
		primitiveUnits: null,
		propagate: null,
		property: Gt,
		r: null,
		radius: null,
		referrerPolicy: null,
		refX: null,
		refY: null,
		rel: Gt,
		rev: Gt,
		renderingIntent: null,
		repeatCount: null,
		repeatDur: null,
		requiredExtensions: Gt,
		requiredFeatures: Gt,
		requiredFonts: Gt,
		requiredFormats: Gt,
		resource: null,
		restart: null,
		result: null,
		rotate: null,
		rx: null,
		ry: null,
		scale: null,
		seed: null,
		shapeRendering: null,
		side: null,
		slope: null,
		snapshotTime: null,
		specularConstant: M,
		specularExponent: M,
		spreadMethod: null,
		spacing: null,
		startOffset: null,
		stdDeviation: null,
		stemh: null,
		stemv: null,
		stitchTiles: null,
		stopColor: null,
		stopOpacity: null,
		strikethroughPosition: M,
		strikethroughThickness: M,
		string: null,
		stroke: null,
		strokeDashArray: Gt,
		strokeDashOffset: null,
		strokeLineCap: null,
		strokeLineJoin: null,
		strokeMiterLimit: M,
		strokeOpacity: M,
		strokeWidth: null,
		style: null,
		surfaceScale: M,
		syncBehavior: null,
		syncBehaviorDefault: null,
		syncMaster: null,
		syncTolerance: null,
		syncToleranceDefault: null,
		systemLanguage: Gt,
		tabIndex: M,
		tableValues: null,
		target: null,
		targetX: M,
		targetY: M,
		textAnchor: null,
		textDecoration: null,
		textRendering: null,
		textLength: null,
		timelineBegin: null,
		title: null,
		transformBehavior: null,
		type: null,
		typeOf: Gt,
		to: null,
		transform: null,
		transformOrigin: null,
		u1: null,
		u2: null,
		underlinePosition: M,
		underlineThickness: M,
		unicode: null,
		unicodeBidi: null,
		unicodeRange: null,
		unitsPerEm: M,
		values: null,
		vAlphabetic: M,
		vMathematical: M,
		vectorEffect: null,
		vHanging: M,
		vIdeographic: M,
		version: null,
		vertAdvY: M,
		vertOriginX: M,
		vertOriginY: M,
		viewBox: null,
		viewTarget: null,
		visibility: null,
		width: null,
		widths: null,
		wordSpacing: null,
		writingMode: null,
		x: null,
		x1: null,
		x2: null,
		xChannelSelector: null,
		xHeight: M,
		y: null,
		y1: null,
		y2: null,
		yChannelSelector: null,
		z: null,
		zoomAndPan: null
	},
	space: "svg",
	transform: Qt
}), nn = Xt({
	properties: {
		xLinkActuate: null,
		xLinkArcRole: null,
		xLinkHref: null,
		xLinkRole: null,
		xLinkShow: null,
		xLinkTitle: null,
		xLinkType: null
	},
	space: "xlink",
	transform(e, t) {
		return "xlink:" + t.slice(5).toLowerCase();
	}
}), rn = Xt({
	attributes: { xmlnsxlink: "xmlns:xlink" },
	properties: {
		xmlnsXLink: null,
		xmlns: null
	},
	space: "xmlns",
	transform: $t
}), an = Xt({
	properties: {
		xmlBase: null,
		xmlLang: null,
		xmlSpace: null
	},
	space: "xml",
	transform(e, t) {
		return "xml:" + t.slice(3).toLowerCase();
	}
}), on = {
	classId: "classID",
	dataType: "datatype",
	itemId: "itemID",
	strokeDashArray: "strokeDasharray",
	strokeDashOffset: "strokeDashoffset",
	strokeLineCap: "strokeLinecap",
	strokeLineJoin: "strokeLinejoin",
	strokeMiterLimit: "strokeMiterlimit",
	typeOf: "typeof",
	xLinkActuate: "xlinkActuate",
	xLinkArcRole: "xlinkArcrole",
	xLinkHref: "xlinkHref",
	xLinkRole: "xlinkRole",
	xLinkShow: "xlinkShow",
	xLinkTitle: "xlinkTitle",
	xLinkType: "xlinkType",
	xmlnsXLink: "xmlnsXlink"
}, sn = /[A-Z]/g, cn = /-[a-z]/g, ln = /^data[-\w.:]+$/i;
function un(e, t) {
	let n = Lt(t), r = t, i = Rt;
	if (n in e.normal) return e.property[e.normal[n]];
	if (n.length > 4 && n.slice(0, 4) === "data" && ln.test(t)) {
		if (t.charAt(4) === "-") {
			let e = t.slice(5).replace(cn, fn);
			r = "data" + e.charAt(0).toUpperCase() + e.slice(1);
		} else {
			let e = t.slice(4);
			if (!cn.test(e)) {
				let n = e.replace(sn, dn);
				n.charAt(0) !== "-" && (n = "-" + n), t = "data" + n;
			}
		}
		i = Jt;
	}
	return new i(r, t);
}
function dn(e) {
	return "-" + e.toLowerCase();
}
function fn(e) {
	return e.charAt(1).toUpperCase();
}
//#endregion
//#region frontend/node_modules/property-information/index.js
var pn = It([
	Zt,
	en,
	nn,
	rn,
	an
], "html"), mn = It([
	Zt,
	tn,
	nn,
	rn,
	an
], "svg");
//#endregion
//#region frontend/node_modules/comma-separated-tokens/index.js
function hn(e) {
	let t = [], n = String(e || ""), r = n.indexOf(","), i = 0, a = !1;
	for (; !a;) {
		r === -1 && (r = n.length, a = !0);
		let e = n.slice(i, r).trim();
		(e || !a) && t.push(e), i = r + 1, r = n.indexOf(",", i);
	}
	return t;
}
function gn(e, t) {
	let n = t || {};
	return (e[e.length - 1] === "" ? [...e, ""] : e).join((n.padRight ? " " : "") + "," + (n.padLeft === !1 ? "" : " ")).trim();
}
//#endregion
//#region frontend/node_modules/hast-util-parse-selector/lib/index.js
var _n = /[#.]/g;
function vn(e, t) {
	let n = e || "", r = {}, i = 0, a, o;
	for (; i < n.length;) {
		_n.lastIndex = i;
		let e = _n.exec(n), t = n.slice(i, e ? e.index : n.length);
		t && (a ? a === "#" ? r.id = t : Array.isArray(r.className) ? r.className.push(t) : r.className = [t] : o = t, i += t.length), e && (a = e[0], i++);
	}
	return {
		type: "element",
		tagName: o || t || "div",
		properties: r,
		children: []
	};
}
//#endregion
//#region frontend/node_modules/space-separated-tokens/index.js
function yn(e) {
	let t = String(e || "").trim();
	return t ? t.split(/[ \t\n\r\f]+/g) : [];
}
function bn(e) {
	return e.join(" ").trim();
}
//#endregion
//#region frontend/node_modules/hastscript/lib/create-h.js
function xn(e, t, n) {
	let r = n ? Dn(n) : void 0;
	function i(n, i, ...a) {
		let o;
		if (n == null) {
			o = {
				type: "root",
				children: []
			};
			let e = i;
			a.unshift(e);
		} else {
			o = vn(n, t);
			let s = o.tagName.toLowerCase(), c = r ? r.get(s) : void 0;
			if (o.tagName = c || s, Sn(i)) a.unshift(i);
			else for (let [t, n] of Object.entries(i)) Cn(e, o.properties, t, n);
		}
		for (let e of a) wn(o.children, e);
		return o.type === "element" && o.tagName === "template" && (o.content = {
			type: "root",
			children: o.children
		}, o.children = []), o;
	}
	return i;
}
function Sn(e) {
	if (typeof e != "object" || !e || Array.isArray(e)) return !0;
	if (typeof e.type != "string") return !1;
	let t = e, n = Object.keys(e);
	for (let e of n) {
		let n = t[e];
		if (n && typeof n == "object") {
			if (!Array.isArray(n)) return !0;
			let e = n;
			for (let t of e) if (typeof t != "number" && typeof t != "string") return !0;
		}
	}
	return !!("children" in e && Array.isArray(e.children));
}
function Cn(e, t, n, r) {
	let i = un(e, n), a;
	if (r != null) {
		if (typeof r == "number") {
			if (Number.isNaN(r)) return;
			a = r;
		} else a = typeof r == "boolean" ? r : typeof r == "string" ? i.spaceSeparated ? yn(r) : i.commaSeparated ? hn(r) : i.commaOrSpaceSeparated ? yn(hn(r).join(" ")) : Tn(i, i.property, r) : Array.isArray(r) ? [...r] : i.property === "style" ? En(r) : String(r);
		if (Array.isArray(a)) {
			let e = [];
			for (let t of a) e.push(Tn(i, i.property, t));
			a = e;
		}
		i.property === "className" && Array.isArray(t.className) && (a = t.className.concat(a)), t[i.property] = a;
	}
}
function wn(e, t) {
	if (t != null) if (typeof t == "number" || typeof t == "string") e.push({
		type: "text",
		value: String(t)
	});
	else if (Array.isArray(t)) for (let n of t) wn(e, n);
	else if (typeof t == "object" && "type" in t) t.type === "root" ? wn(e, t.children) : e.push(t);
	else throw Error("Expected node, nodes, or string, got `" + t + "`");
}
function Tn(e, t, n) {
	if (typeof n == "string") {
		if (e.number && n && !Number.isNaN(Number(n))) return Number(n);
		if ((e.boolean || e.overloadedBoolean) && (n === "" || Lt(n) === Lt(t))) return !0;
	}
	return n;
}
function En(e) {
	let t = [];
	for (let [n, r] of Object.entries(e)) t.push([n, r].join(": "));
	return t.join("; ");
}
function Dn(e) {
	let t = /* @__PURE__ */ new Map();
	for (let n of e) t.set(n.toLowerCase(), n);
	return t;
}
//#endregion
//#region frontend/node_modules/hastscript/lib/svg-case-sensitive-tag-names.js
var On = /* @__PURE__ */ "altGlyph.altGlyphDef.altGlyphItem.animateColor.animateMotion.animateTransform.clipPath.feBlend.feColorMatrix.feComponentTransfer.feComposite.feConvolveMatrix.feDiffuseLighting.feDisplacementMap.feDistantLight.feDropShadow.feFlood.feFuncA.feFuncB.feFuncG.feFuncR.feGaussianBlur.feImage.feMerge.feMergeNode.feMorphology.feOffset.fePointLight.feSpecularLighting.feSpotLight.feTile.feTurbulence.foreignObject.glyphRef.linearGradient.radialGradient.solidColor.textArea.textPath".split("."), kn = xn(pn, "div"), An = xn(mn, "g", On), jn = {
	html: "http://www.w3.org/1999/xhtml",
	mathml: "http://www.w3.org/1998/Math/MathML",
	svg: "http://www.w3.org/2000/svg",
	xlink: "http://www.w3.org/1999/xlink",
	xml: "http://www.w3.org/XML/1998/namespace",
	xmlns: "http://www.w3.org/2000/xmlns/"
};
//#endregion
//#region frontend/node_modules/hast-util-from-dom/lib/index.js
function Mn(e, t) {
	return Nn(e, t || {}) || {
		type: "root",
		children: []
	};
}
function Nn(e, t) {
	let n = Pn(e, t);
	return n && t.afterTransform && t.afterTransform(e, n), n;
}
function Pn(e, t) {
	switch (e.nodeType) {
		case 1: return zn(e, t);
		case 3: return Ln(e);
		case 8: return Rn(e);
		case 9: return Fn(e, t);
		case 10: return In();
		case 11: return Fn(e, t);
		default: return;
	}
}
function Fn(e, t) {
	return {
		type: "root",
		children: Bn(e, t)
	};
}
function In() {
	return { type: "doctype" };
}
function Ln(e) {
	return {
		type: "text",
		value: e.nodeValue || ""
	};
}
function Rn(e) {
	return {
		type: "comment",
		value: e.nodeValue || ""
	};
}
function zn(e, t) {
	let n = e.namespaceURI, r = n === jn.svg ? An : kn, i = n === jn.html ? e.tagName.toLowerCase() : e.tagName, a = n === jn.html && i === "template" ? e.content : e, o = e.getAttributeNames(), s = {}, c = -1;
	for (; ++c < o.length;) s[o[c]] = e.getAttribute(o[c]) || "";
	return r(i, s, Bn(a, t));
}
function Bn(e, t) {
	let n = e.childNodes, r = [], i = -1;
	for (; ++i < n.length;) {
		let e = Nn(n[i], t);
		e !== void 0 && r.push(e);
	}
	return r;
}
//#endregion
//#region frontend/node_modules/hast-util-from-html-isomorphic/lib/browser.js
var Vn = new DOMParser();
function Hn(e, t) {
	return Mn(t?.fragment ? Un(e) : Vn.parseFromString(e, "text/html"));
}
function Un(e) {
	let t = document.createElement("template");
	return t.innerHTML = e, t.content;
}
//#endregion
//#region frontend/node_modules/katex/dist/katex.mjs
var N = class e extends Error {
	constructor(t, n) {
		var r = "KaTeX parse error: " + t, i, a, o = n && n.loc;
		if (o && o.start <= o.end) {
			var s = o.lexer.input;
			i = o.start, a = o.end, i === s.length ? r += " at end of input: " : r += " at position " + (i + 1) + ": ";
			var c = s.slice(i, a).replace(/[^]/g, "$&̲"), l = i > 15 ? "…" + s.slice(i - 15, i) : s.slice(0, i), u = a + 15 < s.length ? s.slice(a, a + 15) + "…" : s.slice(a);
			r += l + c + u;
		}
		super(r), this.name = "ParseError", this.position = void 0, this.length = void 0, this.rawMessage = void 0, Object.setPrototypeOf(this, e.prototype), this.position = i, i != null && a != null && (this.length = a - i), this.rawMessage = t;
	}
}, Wn = /([A-Z])/g, Gn = (e) => e.replace(Wn, "-$1").toLowerCase(), Kn = {
	"&": "&amp;",
	">": "&gt;",
	"<": "&lt;",
	"\"": "&quot;",
	"'": "&#x27;"
}, qn = /[&><"']/g, Jn = (e) => String(e).replace(qn, (e) => Kn[e]), Yn = (e) => e.type === "ordgroup" || e.type === "color" ? e.body.length === 1 ? Yn(e.body[0]) : e : e.type === "font" ? Yn(e.body) : e, Xn = new Set([
	"mathord",
	"textord",
	"atom"
]), Zn = (e) => Xn.has(Yn(e).type), Qn = (e) => {
	var t = /^[\x00-\x20]*([^\\/#?]*?)(:|&#0*58|&#x0*3a|&colon)/i.exec(e);
	return t ? t[2] !== ":" || !/^[a-zA-Z][a-zA-Z0-9+\-.]*$/.test(t[1]) ? null : t[1].toLowerCase() : "_relative";
}, $n = {
	displayMode: {
		type: "boolean",
		description: "Render math in display mode, which puts the math in display style (so \\int and \\sum are large, for example), and centers the math on the page on its own line.",
		cli: "-d, --display-mode"
	},
	output: {
		type: { enum: [
			"htmlAndMathml",
			"html",
			"mathml"
		] },
		description: "Determines the markup language of the output.",
		cli: "-F, --format <type>"
	},
	leqno: {
		type: "boolean",
		description: "Render display math in leqno style (left-justified tags)."
	},
	fleqn: {
		type: "boolean",
		description: "Render display math flush left."
	},
	throwOnError: {
		type: "boolean",
		default: !0,
		cli: "-t, --no-throw-on-error",
		cliDescription: "Render errors (in the color given by --error-color) instead of throwing a ParseError exception when encountering an error."
	},
	errorColor: {
		type: "string",
		default: "#cc0000",
		cli: "-c, --error-color <color>",
		cliDescription: "A color string given in the format 'rgb' or 'rrggbb' (no #). This option determines the color of errors rendered by the -t option.",
		cliProcessor: (e) => "#" + e
	},
	macros: {
		type: "object",
		cli: "-m, --macro <def>",
		cliDescription: "Define custom macro of the form '\\foo:expansion' (use multiple -m arguments for multiple macros).",
		cliDefault: [],
		cliProcessor: (e, t) => (t.push(e), t)
	},
	minRuleThickness: {
		type: "number",
		description: "Specifies a minimum thickness, in ems, for fraction lines, `\\sqrt` top lines, `{array}` vertical lines, `\\hline`, `\\hdashline`, `\\underline`, `\\overline`, and the borders of `\\fbox`, `\\boxed`, and `\\fcolorbox`.",
		processor: (e) => Math.max(0, e),
		cli: "--min-rule-thickness <size>",
		cliProcessor: parseFloat
	},
	colorIsTextColor: {
		type: "boolean",
		description: "Makes \\color behave like LaTeX's 2-argument \\textcolor, instead of LaTeX's one-argument \\color mode change.",
		cli: "-b, --color-is-text-color"
	},
	strict: {
		type: [
			{ enum: [
				"warn",
				"ignore",
				"error"
			] },
			"boolean",
			"function"
		],
		description: "Turn on strict / LaTeX faithfulness mode, which throws an error if the input uses features that are not supported by LaTeX.",
		cli: "-S, --strict",
		cliDefault: !1
	},
	trust: {
		type: ["boolean", "function"],
		description: "Trust the input, enabling all HTML features such as \\url.",
		cli: "-T, --trust"
	},
	maxSize: {
		type: "number",
		default: Infinity,
		description: "If non-zero, all user-specified sizes, e.g. in \\rule{500em}{500em}, will be capped to maxSize ems. Otherwise, elements and spaces can be arbitrarily large",
		processor: (e) => Math.max(0, e),
		cli: "-s, --max-size <n>",
		cliProcessor: parseInt
	},
	maxExpand: {
		type: "number",
		default: 1e3,
		description: "Limit the number of macro expansions to the specified number, to prevent e.g. infinite macro loops. If set to Infinity, the macro expander will try to fully expand as in LaTeX.",
		processor: (e) => Math.max(0, e),
		cli: "-e, --max-expand <n>",
		cliProcessor: (e) => e === "Infinity" ? Infinity : parseInt(e)
	},
	globalGroup: {
		type: "boolean",
		cli: !1
	}
};
function er(e) {
	if (typeof e != "string") return e.enum[0];
	switch (e) {
		case "boolean": return !1;
		case "string": return "";
		case "number": return 0;
		case "object": return {};
		default: throw Error("Unexpected schema type; settings must declare an explicit default.");
	}
}
function tr(e) {
	return e.default === void 0 ? er(Array.isArray(e.type) ? e.type[0] : e.type) : e.default;
}
function nr(e, t, n, r) {
	var i = n[t];
	e[t] = i === void 0 ? tr(r) : r.processor ? r.processor(i) : i;
}
var rr = class {
	constructor(e) {
		e === void 0 && (e = {}), this.displayMode = void 0, this.output = void 0, this.leqno = void 0, this.fleqn = void 0, this.throwOnError = void 0, this.errorColor = void 0, this.macros = void 0, this.minRuleThickness = void 0, this.colorIsTextColor = void 0, this.strict = void 0, this.trust = void 0, this.maxSize = void 0, this.maxExpand = void 0, this.globalGroup = void 0, e ||= {};
		for (var t of Object.keys($n)) {
			var n = $n[t];
			n && nr(this, t, e, n);
		}
	}
	reportNonstrict(e, t, n) {
		var r = this.strict;
		if (typeof r == "function" && (r = r(e, t, n)), !(!r || r === "ignore")) {
			if (r === !0 || r === "error") throw new N("LaTeX-incompatible input and strict mode is set to 'error': " + (t + " [" + e + "]"), n);
			r === "warn" ? typeof console < "u" && console.warn("LaTeX-incompatible input and strict mode is set to 'warn': " + (t + " [" + e + "]")) : typeof console < "u" && console.warn("LaTeX-incompatible input and strict mode is set to " + ("unrecognized '" + r + "': " + t + " [" + e + "]"));
		}
	}
	useStrictBehavior(e, t, n) {
		var r = this.strict;
		if (typeof r == "function") try {
			r = r(e, t, n);
		} catch {
			r = "error";
		}
		return !r || r === "ignore" ? !1 : r === !0 || r === "error" ? !0 : r === "warn" ? (typeof console < "u" && console.warn("LaTeX-incompatible input and strict mode is set to 'warn': " + (t + " [" + e + "]")), !1) : (typeof console < "u" && console.warn("LaTeX-incompatible input and strict mode is set to " + ("unrecognized '" + r + "': " + t + " [" + e + "]")), !1);
	}
	isTrusted(e) {
		if ("url" in e && e.url && !e.protocol) {
			var t = Qn(e.url);
			if (t == null) return !1;
			e.protocol = t;
		}
		return !!(typeof this.trust == "function" ? this.trust(e) : this.trust);
	}
}, ir = class {
	constructor(e, t, n) {
		this.id = void 0, this.size = void 0, this.cramped = void 0, this.id = e, this.size = t, this.cramped = n;
	}
	sup() {
		return pr[mr[this.id]];
	}
	sub() {
		return pr[hr[this.id]];
	}
	fracNum() {
		return pr[gr[this.id]];
	}
	fracDen() {
		return pr[_r[this.id]];
	}
	cramp() {
		return pr[vr[this.id]];
	}
	text() {
		return pr[yr[this.id]];
	}
	isTight() {
		return this.size >= 2;
	}
}, ar = 0, or = 1, sr = 2, cr = 3, lr = 4, ur = 5, dr = 6, fr = 7, pr = [
	new ir(ar, 0, !1),
	new ir(or, 0, !0),
	new ir(sr, 1, !1),
	new ir(cr, 1, !0),
	new ir(lr, 2, !1),
	new ir(ur, 2, !0),
	new ir(dr, 3, !1),
	new ir(fr, 3, !0)
], mr = [
	lr,
	ur,
	lr,
	ur,
	dr,
	fr,
	dr,
	fr
], hr = [
	ur,
	ur,
	ur,
	ur,
	fr,
	fr,
	fr,
	fr
], gr = [
	sr,
	cr,
	lr,
	ur,
	dr,
	fr,
	dr,
	fr
], _r = [
	cr,
	cr,
	ur,
	ur,
	fr,
	fr,
	fr,
	fr
], vr = [
	or,
	or,
	cr,
	cr,
	ur,
	ur,
	fr,
	fr
], yr = [
	ar,
	or,
	sr,
	cr,
	sr,
	cr,
	sr,
	cr
], P = {
	DISPLAY: pr[ar],
	TEXT: pr[sr],
	SCRIPT: pr[lr],
	SCRIPTSCRIPT: pr[dr]
}, br = [
	{
		name: "latin",
		blocks: [[256, 591], [768, 879]]
	},
	{
		name: "cyrillic",
		blocks: [[1024, 1279]]
	},
	{
		name: "armenian",
		blocks: [[1328, 1423]]
	},
	{
		name: "brahmic",
		blocks: [[2304, 4255]]
	},
	{
		name: "georgian",
		blocks: [[4256, 4351]]
	},
	{
		name: "cjk",
		blocks: [
			[12288, 12543],
			[19968, 40879],
			[65280, 65376]
		]
	},
	{
		name: "hangul",
		blocks: [[44032, 55215]]
	}
];
function xr(e) {
	for (var t = 0; t < br.length; t++) for (var n = br[t], r = 0; r < n.blocks.length; r++) {
		var i = n.blocks[r];
		if (e >= i[0] && e <= i[1]) return n.name;
	}
	return null;
}
var Sr = [];
br.forEach((e) => e.blocks.forEach((e) => Sr.push(...e)));
function Cr(e) {
	for (var t = 0; t < Sr.length; t += 2) if (e >= Sr[t] && e <= Sr[t + 1]) return !0;
	return !1;
}
var wr = (e) => e + " " + e, Tr = 80, Er = function(e, t) {
	return "M95," + (622 + e + t) + "\nc-2.7,0,-7.17,-2.7,-13.5,-8c-5.8,-5.3,-9.5,-10,-9.5,-14\nc0,-2,0.3,-3.3,1,-4c1.3,-2.7,23.83,-20.7,67.5,-54\nc44.2,-33.3,65.8,-50.3,66.5,-51c1.3,-1.3,3,-2,5,-2c4.7,0,8.7,3.3,12,10\ns173,378,173,378c0.7,0,35.3,-71,104,-213c68.7,-142,137.5,-285,206.5,-429\nc69,-144,104.5,-217.7,106.5,-221\nl" + e / 2.075 + " -" + e + "\nc5.3,-9.3,12,-14,20,-14\nH400000v" + (40 + e) + "H845.2724\ns-225.272,467,-225.272,467s-235,486,-235,486c-2.7,4.7,-9,7,-19,7\nc-6,0,-10,-1,-12,-3s-194,-422,-194,-422s-65,47,-65,47z\nM" + (834 + e) + " " + t + "h400000v" + (40 + e) + "h-400000z";
}, Dr = function(e, t) {
	return "M263," + (601 + e + t) + "c0.7,0,18,39.7,52,119\nc34,79.3,68.167,158.7,102.5,238c34.3,79.3,51.8,119.3,52.5,120\nc340,-704.7,510.7,-1060.3,512,-1067\nl" + e / 2.084 + " -" + e + "\nc4.7,-7.3,11,-11,19,-11\nH40000v" + (40 + e) + "H1012.3\ns-271.3,567,-271.3,567c-38.7,80.7,-84,175,-136,283c-52,108,-89.167,185.3,-111.5,232\nc-22.3,46.7,-33.8,70.3,-34.5,71c-4.7,4.7,-12.3,7,-23,7s-12,-1,-12,-1\ns-109,-253,-109,-253c-72.7,-168,-109.3,-252,-110,-252c-10.7,8,-22,16.7,-34,26\nc-22,17.3,-33.3,26,-34,26s-26,-26,-26,-26s76,-59,76,-59s76,-60,76,-60z\nM" + (1001 + e) + " " + t + "h400000v" + (40 + e) + "h-400000z";
}, Or = function(e, t) {
	return "M983 " + (10 + e + t) + "\nl" + e / 3.13 + " -" + e + "\nc4,-6.7,10,-10,18,-10 H400000v" + (40 + e) + "\nH1013.1s-83.4,268,-264.1,840c-180.7,572,-277,876.3,-289,913c-4.7,4.7,-12.7,7,-24,7\ns-12,0,-12,0c-1.3,-3.3,-3.7,-11.7,-7,-25c-35.3,-125.3,-106.7,-373.3,-214,-744\nc-10,12,-21,25,-33,39s-32,39,-32,39c-6,-5.3,-15,-14,-27,-26s25,-30,25,-30\nc26.7,-32.7,52,-63,76,-91s52,-60,52,-60s208,722,208,722\nc56,-175.3,126.3,-397.3,211,-666c84.7,-268.7,153.8,-488.2,207.5,-658.5\nc53.7,-170.3,84.5,-266.8,92.5,-289.5z\nM" + (1001 + e) + " " + t + "h400000v" + (40 + e) + "h-400000z";
}, kr = function(e, t) {
	return "M424," + (2398 + e + t) + "\nc-1.3,-0.7,-38.5,-172,-111.5,-514c-73,-342,-109.8,-513.3,-110.5,-514\nc0,-2,-10.7,14.3,-32,49c-4.7,7.3,-9.8,15.7,-15.5,25c-5.7,9.3,-9.8,16,-12.5,20\ns-5,7,-5,7c-4,-3.3,-8.3,-7.7,-13,-13s-13,-13,-13,-13s76,-122,76,-122s77,-121,77,-121\ns209,968,209,968c0,-2,84.7,-361.7,254,-1079c169.3,-717.3,254.7,-1077.7,256,-1081\nl" + e / 4.223 + " -" + e + "c4,-6.7,10,-10,18,-10 H400000\nv" + (40 + e) + "H1014.6\ns-87.3,378.7,-272.6,1166c-185.3,787.3,-279.3,1182.3,-282,1185\nc-2,6,-10,9,-24,9\nc-8,0,-12,-0.7,-12,-2z M" + (1001 + e) + " " + t + "\nh400000v" + (40 + e) + "h-400000z";
}, Ar = function(e, t) {
	return "M473," + (2713 + e + t) + "\nc339.3,-1799.3,509.3,-2700,510,-2702 l" + e / 5.298 + " -" + e + "\nc3.3,-7.3,9.3,-11,18,-11 H400000v" + (40 + e) + "H1017.7\ns-90.5,478,-276.2,1466c-185.7,988,-279.5,1483,-281.5,1485c-2,6,-10,9,-24,9\nc-8,0,-12,-0.7,-12,-2c0,-1.3,-5.3,-32,-16,-92c-50.7,-293.3,-119.7,-693.3,-207,-1200\nc0,-1.3,-5.3,8.7,-16,30c-10.7,21.3,-21.3,42.7,-32,64s-16,33,-16,33s-26,-26,-26,-26\ns76,-153,76,-153s77,-151,77,-151c0.7,0.7,35.7,202,105,604c67.3,400.7,102,602.7,104,\n606zM" + (1001 + e) + " " + t + "h400000v" + (40 + e) + "H1017.7z";
}, jr = function(e) {
	var t = e / 2;
	return "M400000 " + e + " H0 L" + t + " 0 l65 45 L145 " + (e - 80) + " H400000z";
}, Mr = function(e, t, n) {
	var r = n - 54 - t - e;
	return "M702 " + (e + t) + "H400000" + (40 + e) + "\nH742v" + r + "l-4 4-4 4c-.667.7 -2 1.5-4 2.5s-4.167 1.833-6.5 2.5-5.5 1-9.5 1\nh-12l-28-84c-16.667-52-96.667 -294.333-240-727l-212 -643 -85 170\nc-4-3.333-8.333-7.667-13 -13l-13-13l77-155 77-156c66 199.333 139 419.667\n219 661 l218 661zM702 " + t + "H400000v" + (40 + e) + "H742z";
}, Nr = function(e, t, n) {
	t = 1e3 * t;
	var r = "";
	switch (e) {
		case "sqrtMain":
			r = Er(t, Tr);
			break;
		case "sqrtSize1":
			r = Dr(t, Tr);
			break;
		case "sqrtSize2":
			r = Or(t, Tr);
			break;
		case "sqrtSize3":
			r = kr(t, Tr);
			break;
		case "sqrtSize4":
			r = Ar(t, Tr);
			break;
		case "sqrtTall": r = Mr(t, Tr, n);
	}
	return r;
}, Pr = function(e, t) {
	switch (e) {
		case "⎜": return wr("M291 0 H417 V" + t + " H291z");
		case "∣": return wr("M145 0 H188 V" + t + " H145z");
		case "∥": return wr("M145 0 H188 V" + t + " H145z") + wr("M367 0 H410 V" + t + " H367z");
		case "⎟": return wr("M457 0 H583 V" + t + " H457z");
		case "⎢": return wr("M319 0 H403 V" + t + " H319z");
		case "⎥": return wr("M263 0 H347 V" + t + " H263z");
		case "⎪": return wr("M384 0 H504 V" + t + " H384z");
		case "⏐": return wr("M312 0 H355 V" + t + " H312z");
		case "‖": return wr("M257 0 H300 V" + t + " H257z") + wr("M478 0 H521 V" + t + " H478z");
		default: return "";
	}
}, Fr = {
	doubleleftarrow: "M262 157\nl10-10c34-36 62.7-77 86-123 3.3-8 5-13.3 5-16 0-5.3-6.7-8-20-8-7.3\n 0-12.2.5-14.5 1.5-2.3 1-4.8 4.5-7.5 10.5-49.3 97.3-121.7 169.3-217 216-28\n 14-57.3 25-88 33-6.7 2-11 3.8-13 5.5-2 1.7-3 4.2-3 7.5s1 5.8 3 7.5\nc2 1.7 6.3 3.5 13 5.5 68 17.3 128.2 47.8 180.5 91.5 52.3 43.7 93.8 96.2 124.5\n 157.5 9.3 8 15.3 12.3 18 13h6c12-.7 18-4 18-10 0-2-1.7-7-5-15-23.3-46-52-87\n-86-123l-10-10h399738v-40H218c328 0 0 0 0 0l-10-8c-26.7-20-65.7-43-117-69 2.7\n-2 6-3.7 10-5 36.7-16 72.3-37.3 107-64l10-8h399782v-40z\nm8 0v40h399730v-40zm0 194v40h399730v-40z",
	doublerightarrow: "M399738 392l\n-10 10c-34 36-62.7 77-86 123-3.3 8-5 13.3-5 16 0 5.3 6.7 8 20 8 7.3 0 12.2-.5\n 14.5-1.5 2.3-1 4.8-4.5 7.5-10.5 49.3-97.3 121.7-169.3 217-216 28-14 57.3-25 88\n-33 6.7-2 11-3.8 13-5.5 2-1.7 3-4.2 3-7.5s-1-5.8-3-7.5c-2-1.7-6.3-3.5-13-5.5-68\n-17.3-128.2-47.8-180.5-91.5-52.3-43.7-93.8-96.2-124.5-157.5-9.3-8-15.3-12.3-18\n-13h-6c-12 .7-18 4-18 10 0 2 1.7 7 5 15 23.3 46 52 87 86 123l10 10H0v40h399782\nc-328 0 0 0 0 0l10 8c26.7 20 65.7 43 117 69-2.7 2-6 3.7-10 5-36.7 16-72.3 37.3\n-107 64l-10 8H0v40zM0 157v40h399730v-40zm0 194v40h399730v-40z",
	leftarrow: "M400000 241H110l3-3c68.7-52.7 113.7-120\n 135-202 4-14.7 6-23 6-25 0-7.3-7-11-21-11-8 0-13.2.8-15.5 2.5-2.3 1.7-4.2 5.8\n-5.5 12.5-1.3 4.7-2.7 10.3-4 17-12 48.7-34.8 92-68.5 130S65.3 228.3 18 247\nc-10 4-16 7.7-18 11 0 8.7 6 14.3 18 17 47.3 18.7 87.8 47 121.5 85S196 441.3 208\n 490c.7 2 1.3 5 2 9s1.2 6.7 1.5 8c.3 1.3 1 3.3 2 6s2.2 4.5 3.5 5.5c1.3 1 3.3\n 1.8 6 2.5s6 1 10 1c14 0 21-3.7 21-11 0-2-2-10.3-6-25-20-79.3-65-146.7-135-202\n l-3-3h399890zM100 241v40h399900v-40z",
	leftbrace: "M6 548l-6-6v-35l6-11c56-104 135.3-181.3 238-232 57.3-28.7 117\n-45 179-50h399577v120H403c-43.3 7-81 15-113 26-100.7 33-179.7 91-237 174-2.7\n 5-6 9-10 13-.7 1-7.3 1-20 1H6z",
	leftbraceunder: "M0 6l6-6h17c12.688 0 19.313.3 20 1 4 4 7.313 8.3 10 13\n 35.313 51.3 80.813 93.8 136.5 127.5 55.688 33.7 117.188 55.8 184.5 66.5.688\n 0 2 .3 4 1 18.688 2.7 76 4.3 172 5h399450v120H429l-6-1c-124.688-8-235-61.7\n-331-161C60.687 138.7 32.312 99.3 7 54L0 41V6z",
	leftgroup: "M400000 80\nH435C64 80 168.3 229.4 21 260c-5.9 1.2-18 0-18 0-2 0-3-1-3-3v-38C76 61 257 0\n 435 0h399565z",
	leftgroupunder: "M400000 262\nH435C64 262 168.3 112.6 21 82c-5.9-1.2-18 0-18 0-2 0-3 1-3 3v38c76 158 257 219\n 435 219h399565z",
	leftharpoon: "M0 267c.7 5.3 3 10 7 14h399993v-40H93c3.3\n-3.3 10.2-9.5 20.5-18.5s17.8-15.8 22.5-20.5c50.7-52 88-110.3 112-175 4-11.3 5\n-18.3 3-21-1.3-4-7.3-6-18-6-8 0-13 .7-15 2s-4.7 6.7-8 16c-42 98.7-107.3 174.7\n-196 228-6.7 4.7-10.7 8-12 10-1.3 2-2 5.7-2 11zm100-26v40h399900v-40z",
	leftharpoonplus: "M0 267c.7 5.3 3 10 7 14h399993v-40H93c3.3-3.3 10.2-9.5\n 20.5-18.5s17.8-15.8 22.5-20.5c50.7-52 88-110.3 112-175 4-11.3 5-18.3 3-21-1.3\n-4-7.3-6-18-6-8 0-13 .7-15 2s-4.7 6.7-8 16c-42 98.7-107.3 174.7-196 228-6.7 4.7\n-10.7 8-12 10-1.3 2-2 5.7-2 11zm100-26v40h399900v-40zM0 435v40h400000v-40z\nm0 0v40h400000v-40z",
	leftharpoondown: "M7 241c-4 4-6.333 8.667-7 14 0 5.333.667 9 2 11s5.333\n 5.333 12 10c90.667 54 156 130 196 228 3.333 10.667 6.333 16.333 9 17 2 .667 5\n 1 9 1h5c10.667 0 16.667-2 18-6 2-2.667 1-9.667-3-21-32-87.333-82.667-157.667\n-152-211l-3-3h399907v-40zM93 281 H400000 v-40L7 241z",
	leftharpoondownplus: "M7 435c-4 4-6.3 8.7-7 14 0 5.3.7 9 2 11s5.3 5.3 12\n 10c90.7 54 156 130 196 228 3.3 10.7 6.3 16.3 9 17 2 .7 5 1 9 1h5c10.7 0 16.7\n-2 18-6 2-2.7 1-9.7-3-21-32-87.3-82.7-157.7-152-211l-3-3h399907v-40H7zm93 0\nv40h399900v-40zM0 241v40h399900v-40zm0 0v40h399900v-40z",
	lefthook: "M400000 281 H103s-33-11.2-61-33.5S0 197.3 0 164s14.2-61.2 42.5\n-83.5C70.8 58.2 104 47 142 47 c16.7 0 25 6.7 25 20 0 12-8.7 18.7-26 20-40 3.3\n-68.7 15.7-86 37-10 12-15 25.3-15 40 0 22.7 9.8 40.7 29.5 54 19.7 13.3 43.5 21\n 71.5 23h399859zM103 281v-40h399897v40z",
	leftlinesegment: wr("M40 281 V428 H0 V94 H40 V241 H400000 v40z"),
	leftbracketunder: wr("M0 0 h120 V290 H399995 v120 H0z"),
	leftbracketover: wr("M0 440 h120 V150 H399995 v-120 H0z"),
	leftmapsto: wr("M40 281 V448H0V74H40V241H400000v40z"),
	leftToFrom: "M0 147h400000v40H0zm0 214c68 40 115.7 95.7 143 167h22c15.3 0 23\n-.3 23-1 0-1.3-5.3-13.7-16-37-18-35.3-41.3-69-70-101l-7-8h399905v-40H95l7-8\nc28.7-32 52-65.7 70-101 10.7-23.3 16-35.7 16-37 0-.7-7.7-1-23-1h-22C115.7 265.3\n 68 321 0 361zm0-174v-40h399900v40zm100 154v40h399900v-40z",
	longequal: wr("M0 50 h400000 v40H0z m0 194h40000v40H0z"),
	midbrace: "M200428 334\nc-100.7-8.3-195.3-44-280-108-55.3-42-101.7-93-139-153l-9-14c-2.7 4-5.7 8.7-9 14\n-53.3 86.7-123.7 153-211 199-66.7 36-137.3 56.3-212 62H0V214h199568c178.3-11.7\n 311.7-78.3 403-201 6-8 9.7-12 11-12 .7-.7 6.7-1 18-1s17.3.3 18 1c1.3 0 5 4 11\n 12 44.7 59.3 101.3 106.3 170 141s145.3 54.3 229 60h199572v120z",
	midbraceunder: "M199572 214\nc100.7 8.3 195.3 44 280 108 55.3 42 101.7 93 139 153l9 14c2.7-4 5.7-8.7 9-14\n 53.3-86.7 123.7-153 211-199 66.7-36 137.3-56.3 212-62h199568v120H200432c-178.3\n 11.7-311.7 78.3-403 201-6 8-9.7 12-11 12-.7.7-6.7 1-18 1s-17.3-.3-18-1c-1.3 0\n-5-4-11-12-44.7-59.3-101.3-106.3-170-141s-145.3-54.3-229-60H0V214z",
	oiintSize1: "M512.6 71.6c272.6 0 320.3 106.8 320.3 178.2 0 70.8-47.7 177.6\n-320.3 177.6S193.1 320.6 193.1 249.8c0-71.4 46.9-178.2 319.5-178.2z\nm368.1 178.2c0-86.4-60.9-215.4-368.1-215.4-306.4 0-367.3 129-367.3 215.4 0 85.8\n60.9 214.8 367.3 214.8 307.2 0 368.1-129 368.1-214.8z",
	oiintSize2: "M757.8 100.1c384.7 0 451.1 137.6 451.1 230 0 91.3-66.4 228.8\n-451.1 228.8-386.3 0-452.7-137.5-452.7-228.8 0-92.4 66.4-230 452.7-230z\nm502.4 230c0-111.2-82.4-277.2-502.4-277.2s-504 166-504 277.2\nc0 110 84 276 504 276s502.4-166 502.4-276z",
	oiiintSize1: "M681.4 71.6c408.9 0 480.5 106.8 480.5 178.2 0 70.8-71.6 177.6\n-480.5 177.6S202.1 320.6 202.1 249.8c0-71.4 70.5-178.2 479.3-178.2z\nm525.8 178.2c0-86.4-86.8-215.4-525.7-215.4-437.9 0-524.7 129-524.7 215.4 0\n85.8 86.8 214.8 524.7 214.8 438.9 0 525.7-129 525.7-214.8z",
	oiiintSize2: "M1021.2 53c603.6 0 707.8 165.8 707.8 277.2 0 110-104.2 275.8\n-707.8 275.8-606 0-710.2-165.8-710.2-275.8C311 218.8 415.2 53 1021.2 53z\nm770.4 277.1c0-131.2-126.4-327.6-770.5-327.6S248.4 198.9 248.4 330.1\nc0 130 128.8 326.4 772.7 326.4s770.5-196.4 770.5-326.4z",
	rightarrow: "M0 241v40h399891c-47.3 35.3-84 78-110 128\n-16.7 32-27.7 63.7-33 95 0 1.3-.2 2.7-.5 4-.3 1.3-.5 2.3-.5 3 0 7.3 6.7 11 20\n 11 8 0 13.2-.8 15.5-2.5 2.3-1.7 4.2-5.5 5.5-11.5 2-13.3 5.7-27 11-41 14.7-44.7\n 39-84.5 73-119.5s73.7-60.2 119-75.5c6-2 9-5.7 9-11s-3-9-9-11c-45.3-15.3-85\n-40.5-119-75.5s-58.3-74.8-73-119.5c-4.7-14-8.3-27.3-11-40-1.3-6.7-3.2-10.8-5.5\n-12.5-2.3-1.7-7.5-2.5-15.5-2.5-14 0-21 3.7-21 11 0 2 2 10.3 6 25 20.7 83.3 67\n 151.7 139 205zm0 0v40h399900v-40z",
	rightbrace: "M400000 542l\n-6 6h-17c-12.7 0-19.3-.3-20-1-4-4-7.3-8.3-10-13-35.3-51.3-80.8-93.8-136.5-127.5\ns-117.2-55.8-184.5-66.5c-.7 0-2-.3-4-1-18.7-2.7-76-4.3-172-5H0V214h399571l6 1\nc124.7 8 235 61.7 331 161 31.3 33.3 59.7 72.7 85 118l7 13v35z",
	rightbraceunder: "M399994 0l6 6v35l-6 11c-56 104-135.3 181.3-238 232-57.3\n 28.7-117 45-179 50H-300V214h399897c43.3-7 81-15 113-26 100.7-33 179.7-91 237\n-174 2.7-5 6-9 10-13 .7-1 7.3-1 20-1h17z",
	rightgroup: "M0 80h399565c371 0 266.7 149.4 414 180 5.9 1.2 18 0 18 0 2 0\n 3-1 3-3v-38c-76-158-257-219-435-219H0z",
	rightgroupunder: "M0 262h399565c371 0 266.7-149.4 414-180 5.9-1.2 18 0 18\n 0 2 0 3 1 3 3v38c-76 158-257 219-435 219H0z",
	rightharpoon: "M0 241v40h399993c4.7-4.7 7-9.3 7-14 0-9.3\n-3.7-15.3-11-18-92.7-56.7-159-133.7-199-231-3.3-9.3-6-14.7-8-16-2-1.3-7-2-15-2\n-10.7 0-16.7 2-18 6-2 2.7-1 9.7 3 21 15.3 42 36.7 81.8 64 119.5 27.3 37.7 58\n 69.2 92 94.5zm0 0v40h399900v-40z",
	rightharpoonplus: "M0 241v40h399993c4.7-4.7 7-9.3 7-14 0-9.3-3.7-15.3-11\n-18-92.7-56.7-159-133.7-199-231-3.3-9.3-6-14.7-8-16-2-1.3-7-2-15-2-10.7 0-16.7\n 2-18 6-2 2.7-1 9.7 3 21 15.3 42 36.7 81.8 64 119.5 27.3 37.7 58 69.2 92 94.5z\nm0 0v40h399900v-40z m100 194v40h399900v-40zm0 0v40h399900v-40z",
	rightharpoondown: "M399747 511c0 7.3 6.7 11 20 11 8 0 13-.8 15-2.5s4.7-6.8\n 8-15.5c40-94 99.3-166.3 178-217 13.3-8 20.3-12.3 21-13 5.3-3.3 8.5-5.8 9.5\n-7.5 1-1.7 1.5-5.2 1.5-10.5s-2.3-10.3-7-15H0v40h399908c-34 25.3-64.7 57-92 95\n-27.3 38-48.7 77.7-64 119-3.3 8.7-5 14-5 16zM0 241v40h399900v-40z",
	rightharpoondownplus: "M399747 705c0 7.3 6.7 11 20 11 8 0 13-.8\n 15-2.5s4.7-6.8 8-15.5c40-94 99.3-166.3 178-217 13.3-8 20.3-12.3 21-13 5.3-3.3\n 8.5-5.8 9.5-7.5 1-1.7 1.5-5.2 1.5-10.5s-2.3-10.3-7-15H0v40h399908c-34 25.3\n-64.7 57-92 95-27.3 38-48.7 77.7-64 119-3.3 8.7-5 14-5 16zM0 435v40h399900v-40z\nm0-194v40h400000v-40zm0 0v40h400000v-40z",
	righthook: "M399859 241c-764 0 0 0 0 0 40-3.3 68.7-15.7 86-37 10-12 15-25.3\n 15-40 0-22.7-9.8-40.7-29.5-54-19.7-13.3-43.5-21-71.5-23-17.3-1.3-26-8-26-20 0\n-13.3 8.7-20 26-20 38 0 71 11.2 99 33.5 0 0 7 5.6 21 16.7 14 11.2 21 33.5 21\n 66.8s-14 61.2-42 83.5c-28 22.3-61 33.5-99 33.5L0 241z M0 281v-40h399859v40z",
	rightlinesegment: wr("M399960 241 V94 h40 V428 h-40 V281 H0 v-40z"),
	rightbracketunder: wr("M399995 0 h-120 V290 H0 v120 H400000z"),
	rightbracketover: wr("M399995 440 h-120 V150 H0 v-120 H399995z"),
	rightToFrom: "M400000 167c-70.7-42-118-97.7-142-167h-23c-15.3 0-23 .3-23\n 1 0 1.3 5.3 13.7 16 37 18 35.3 41.3 69 70 101l7 8H0v40h399905l-7 8c-28.7 32\n-52 65.7-70 101-10.7 23.3-16 35.7-16 37 0 .7 7.7 1 23 1h23c24-69.3 71.3-125 142\n-167z M100 147v40h399900v-40zM0 341v40h399900v-40z",
	twoheadleftarrow: "M0 167c68 40\n 115.7 95.7 143 167h22c15.3 0 23-.3 23-1 0-1.3-5.3-13.7-16-37-18-35.3-41.3-69\n-70-101l-7-8h125l9 7c50.7 39.3 85 86 103 140h46c0-4.7-6.3-18.7-19-42-18-35.3\n-40-67.3-66-96l-9-9h399716v-40H284l9-9c26-28.7 48-60.7 66-96 12.7-23.333 19\n-37.333 19-42h-46c-18 54-52.3 100.7-103 140l-9 7H95l7-8c28.7-32 52-65.7 70-101\n 10.7-23.333 16-35.7 16-37 0-.7-7.7-1-23-1h-22C115.7 71.3 68 127 0 167z",
	twoheadrightarrow: "M400000 167\nc-68-40-115.7-95.7-143-167h-22c-15.3 0-23 .3-23 1 0 1.3 5.3 13.7 16 37 18 35.3\n 41.3 69 70 101l7 8h-125l-9-7c-50.7-39.3-85-86-103-140h-46c0 4.7 6.3 18.7 19 42\n 18 35.3 40 67.3 66 96l9 9H0v40h399716l-9 9c-26 28.7-48 60.7-66 96-12.7 23.333\n-19 37.333-19 42h46c18-54 52.3-100.7 103-140l9-7h125l-7 8c-28.7 32-52 65.7-70\n 101-10.7 23.333-16 35.7-16 37 0 .7 7.7 1 23 1h22c27.3-71.3 75-127 143-167z",
	tilde1: "M200 55.538c-77 0-168 73.953-177 73.953-3 0-7\n-2.175-9-5.437L2 97c-1-2-2-4-2-6 0-4 2-7 5-9l20-12C116 12 171 0 207 0c86 0\n 114 68 191 68 78 0 168-68 177-68 4 0 7 2 9 5l12 19c1 2.175 2 4.35 2 6.525 0\n 4.35-2 7.613-5 9.788l-19 13.05c-92 63.077-116.937 75.308-183 76.128\n-68.267.847-113-73.952-191-73.952z",
	tilde2: "M344 55.266c-142 0-300.638 81.316-311.5 86.418\n-8.01 3.762-22.5 10.91-23.5 5.562L1 120c-1-2-1-3-1-4 0-5 3-9 8-10l18.4-9C160.9\n 31.9 283 0 358 0c148 0 188 122 331 122s314-97 326-97c4 0 8 2 10 7l7 21.114\nc1 2.14 1 3.21 1 4.28 0 5.347-3 9.626-7 10.696l-22.3 12.622C852.6 158.372 751\n 181.476 676 181.476c-149 0-189-126.21-332-126.21z",
	tilde3: "M786 59C457 59 32 175.242 13 175.242c-6 0-10-3.457\n-11-10.37L.15 138c-1-7 3-12 10-13l19.2-6.4C378.4 40.7 634.3 0 804.3 0c337 0\n 411.8 157 746.8 157 328 0 754-112 773-112 5 0 10 3 11 9l1 14.075c1 8.066-.697\n 16.595-6.697 17.492l-21.052 7.31c-367.9 98.146-609.15 122.696-778.15 122.696\n -338 0-409-156.573-744-156.573z",
	tilde4: "M786 58C457 58 32 177.487 13 177.487c-6 0-10-3.345\n-11-10.035L.15 143c-1-7 3-12 10-13l22-6.7C381.2 35 637.15 0 807.15 0c337 0 409\n 177 744 177 328 0 754-127 773-127 5 0 10 3 11 9l1 14.794c1 7.805-3 13.38-9\n 14.495l-20.7 5.574c-366.85 99.79-607.3 139.372-776.3 139.372-338 0-409\n -175.236-744-175.236z",
	vec: "M377 20c0-5.333 1.833-10 5.5-14S391 0 397 0c4.667 0 8.667 1.667 12 5\n3.333 2.667 6.667 9 10 19 6.667 24.667 20.333 43.667 41 57 7.333 4.667 11\n10.667 11 18 0 6-1 10-3 12s-6.667 5-14 9c-28.667 14.667-53.667 35.667-75 63\n-1.333 1.333-3.167 3.5-5.5 6.5s-4 4.833-5 5.5c-1 .667-2.5 1.333-4.5 2s-4.333 1\n-7 1c-4.667 0-9.167-1.833-13.5-5.5S337 184 337 178c0-12.667 15.667-32.333 47-59\nH213l-171-1c-8.667-6-13-12.333-13-19 0-4.667 4.333-11.333 13-20h359\nc-16-25.333-24-45-24-59z",
	widehat1: "M529 0h5l519 115c5 1 9 5 9 10 0 1-1 2-1 3l-4 22\nc-1 5-5 9-11 9h-2L532 67 19 159h-2c-5 0-9-4-11-9l-5-22c-1-6 2-12 8-13z",
	widehat2: "M1181 0h2l1171 176c6 0 10 5 10 11l-2 23c-1 6-5 10\n-11 10h-1L1182 67 15 220h-1c-6 0-10-4-11-10l-2-23c-1-6 4-11 10-11z",
	widehat3: "M1181 0h2l1171 236c6 0 10 5 10 11l-2 23c-1 6-5 10\n-11 10h-1L1182 67 15 280h-1c-6 0-10-4-11-10l-2-23c-1-6 4-11 10-11z",
	widehat4: "M1181 0h2l1171 296c6 0 10 5 10 11l-2 23c-1 6-5 10\n-11 10h-1L1182 67 15 340h-1c-6 0-10-4-11-10l-2-23c-1-6 4-11 10-11z",
	widecheck1: "M529,159h5l519,-115c5,-1,9,-5,9,-10c0,-1,-1,-2,-1,-3l-4,-22c-1,\n-5,-5,-9,-11,-9h-2l-512,92l-513,-92h-2c-5,0,-9,4,-11,9l-5,22c-1,6,2,12,8,13z",
	widecheck2: "M1181,220h2l1171,-176c6,0,10,-5,10,-11l-2,-23c-1,-6,-5,-10,\n-11,-10h-1l-1168,153l-1167,-153h-1c-6,0,-10,4,-11,10l-2,23c-1,6,4,11,10,11z",
	widecheck3: "M1181,280h2l1171,-236c6,0,10,-5,10,-11l-2,-23c-1,-6,-5,-10,\n-11,-10h-1l-1168,213l-1167,-213h-1c-6,0,-10,4,-11,10l-2,23c-1,6,4,11,10,11z",
	widecheck4: "M1181,340h2l1171,-296c6,0,10,-5,10,-11l-2,-23c-1,-6,-5,-10,\n-11,-10h-1l-1168,273l-1167,-273h-1c-6,0,-10,4,-11,10l-2,23c-1,6,4,11,10,11z",
	baraboveleftarrow: "M400000 620h-399890l3 -3c68.7 -52.7 113.7 -120 135 -202\nc4 -14.7 6 -23 6 -25c0 -7.3 -7 -11 -21 -11c-8 0 -13.2 0.8 -15.5 2.5\nc-2.3 1.7 -4.2 5.8 -5.5 12.5c-1.3 4.7 -2.7 10.3 -4 17c-12 48.7 -34.8 92 -68.5 130\ns-74.2 66.3 -121.5 85c-10 4 -16 7.7 -18 11c0 8.7 6 14.3 18 17c47.3 18.7 87.8 47\n121.5 85s56.5 81.3 68.5 130c0.7 2 1.3 5 2 9s1.2 6.7 1.5 8c0.3 1.3 1 3.3 2 6\ns2.2 4.5 3.5 5.5c1.3 1 3.3 1.8 6 2.5s6 1 10 1c14 0 21 -3.7 21 -11\nc0 -2 -2 -10.3 -6 -25c-20 -79.3 -65 -146.7 -135 -202l-3 -3h399890z\nM100 620v40h399900v-40z M0 241v40h399900v-40zM0 241v40h399900v-40z",
	rightarrowabovebar: "M0 241v40h399891c-47.3 35.3-84 78-110 128-16.7 32\n-27.7 63.7-33 95 0 1.3-.2 2.7-.5 4-.3 1.3-.5 2.3-.5 3 0 7.3 6.7 11 20 11 8 0\n13.2-.8 15.5-2.5 2.3-1.7 4.2-5.5 5.5-11.5 2-13.3 5.7-27 11-41 14.7-44.7 39\n-84.5 73-119.5s73.7-60.2 119-75.5c6-2 9-5.7 9-11s-3-9-9-11c-45.3-15.3-85-40.5\n-119-75.5s-58.3-74.8-73-119.5c-4.7-14-8.3-27.3-11-40-1.3-6.7-3.2-10.8-5.5\n-12.5-2.3-1.7-7.5-2.5-15.5-2.5-14 0-21 3.7-21 11 0 2 2 10.3 6 25 20.7 83.3 67\n151.7 139 205zm96 379h399894v40H0zm0 0h399904v40H0z",
	baraboveshortleftharpoon: "M507,435c-4,4,-6.3,8.7,-7,14c0,5.3,0.7,9,2,11\nc1.3,2,5.3,5.3,12,10c90.7,54,156,130,196,228c3.3,10.7,6.3,16.3,9,17\nc2,0.7,5,1,9,1c0,0,5,0,5,0c10.7,0,16.7,-2,18,-6c2,-2.7,1,-9.7,-3,-21\nc-32,-87.3,-82.7,-157.7,-152,-211c0,0,-3,-3,-3,-3l399351,0l0,-40\nc-398570,0,-399437,0,-399437,0z M593 435 v40 H399500 v-40z\nM0 281 v-40 H399908 v40z M0 281 v-40 H399908 v40z",
	rightharpoonaboveshortbar: "M0,241 l0,40c399126,0,399993,0,399993,0\nc4.7,-4.7,7,-9.3,7,-14c0,-9.3,-3.7,-15.3,-11,-18c-92.7,-56.7,-159,-133.7,-199,\n-231c-3.3,-9.3,-6,-14.7,-8,-16c-2,-1.3,-7,-2,-15,-2c-10.7,0,-16.7,2,-18,6\nc-2,2.7,-1,9.7,3,21c15.3,42,36.7,81.8,64,119.5c27.3,37.7,58,69.2,92,94.5z\nM0 241 v40 H399908 v-40z M0 475 v-40 H399500 v40z M0 475 v-40 H399500 v40z",
	shortbaraboveleftharpoon: "M7,435c-4,4,-6.3,8.7,-7,14c0,5.3,0.7,9,2,11\nc1.3,2,5.3,5.3,12,10c90.7,54,156,130,196,228c3.3,10.7,6.3,16.3,9,17c2,0.7,5,1,9,\n1c0,0,5,0,5,0c10.7,0,16.7,-2,18,-6c2,-2.7,1,-9.7,-3,-21c-32,-87.3,-82.7,-157.7,\n-152,-211c0,0,-3,-3,-3,-3l399907,0l0,-40c-399126,0,-399993,0,-399993,0z\nM93 435 v40 H400000 v-40z M500 241 v40 H400000 v-40z M500 241 v40 H400000 v-40z",
	shortrightharpoonabovebar: "M53,241l0,40c398570,0,399437,0,399437,0\nc4.7,-4.7,7,-9.3,7,-14c0,-9.3,-3.7,-15.3,-11,-18c-92.7,-56.7,-159,-133.7,-199,\n-231c-3.3,-9.3,-6,-14.7,-8,-16c-2,-1.3,-7,-2,-15,-2c-10.7,0,-16.7,2,-18,6\nc-2,2.7,-1,9.7,3,21c15.3,42,36.7,81.8,64,119.5c27.3,37.7,58,69.2,92,94.5z\nM500 241 v40 H399408 v-40z M500 435 v40 H400000 v-40z"
}, Ir = function(e, t) {
	switch (e) {
		case "lbrack": return "M403 1759 V84 H666 V0 H319 V1759 v" + t + " v1759 v84 h347 v-84\nH403z M403 1759 V0 H319 V1759 v" + t + " v1759 v84 h84z";
		case "rbrack": return "M347 1759 V0 H0 V84 H263 V1759 v" + t + " v1759 H0 v84 H347z\nM347 1759 V0 H263 V1759 v" + t + " v1759 h84z";
		case "vert": return "M145 15 v585 v" + t + " v585 c2.667,10,9.667,15,21,15\nc10,0,16.667,-5,20,-15 v-585 v" + -t + " v-585 c-2.667,-10,-9.667,-15,-21,-15\nc-10,0,-16.667,5,-20,15z M188 15 H145 v585 v" + t + " v585 h43z";
		case "doublevert": return "M145 15 v585 v" + t + " v585 c2.667,10,9.667,15,21,15\nc10,0,16.667,-5,20,-15 v-585 v" + -t + " v-585 c-2.667,-10,-9.667,-15,-21,-15\nc-10,0,-16.667,5,-20,15z M188 15 H145 v585 v" + t + " v585 h43z\nM367 15 v585 v" + t + " v585 c2.667,10,9.667,15,21,15\nc10,0,16.667,-5,20,-15 v-585 v" + -t + " v-585 c-2.667,-10,-9.667,-15,-21,-15\nc-10,0,-16.667,5,-20,15z M410 15 H367 v585 v" + t + " v585 h43z";
		case "lfloor": return "M319 602 V0 H403 V602 v" + t + " v1715 h263 v84 H319z\nMM319 602 V0 H403 V602 v" + t + " v1715 H319z";
		case "rfloor": return "M319 602 V0 H403 V602 v" + t + " v1799 H0 v-84 H319z\nMM319 602 V0 H403 V602 v" + t + " v1715 H319z";
		case "lceil": return "M403 1759 V84 H666 V0 H319 V1759 v" + t + " v602 h84z\nM403 1759 V0 H319 V1759 v" + t + " v602 h84z";
		case "rceil": return "M347 1759 V0 H0 V84 H263 V1759 v" + t + " v602 h84z\nM347 1759 V0 h-84 V1759 v" + t + " v602 h84z";
		case "lparen": return "M863,9c0,-2,-2,-5,-6,-9c0,0,-17,0,-17,0c-12.7,0,-19.3,0.3,-20,1\nc-5.3,5.3,-10.3,11,-15,17c-242.7,294.7,-395.3,682,-458,1162c-21.3,163.3,-33.3,349,\n-36,557 l0," + (t + 84) + "c0.2,6,0,26,0,60c2,159.3,10,310.7,24,454c53.3,528,210,\n949.7,470,1265c4.7,6,9.7,11.7,15,17c0.7,0.7,7,1,19,1c0,0,18,0,18,0c4,-4,6,-7,6,-9\nc0,-2.7,-3.3,-8.7,-10,-18c-135.3,-192.7,-235.5,-414.3,-300.5,-665c-65,-250.7,-102.5,\n-544.7,-112.5,-882c-2,-104,-3,-167,-3,-189\nl0,-" + (t + 92) + "c0,-162.7,5.7,-314,17,-454c20.7,-272,63.7,-513,129,-723c65.3,\n-210,155.3,-396.3,270,-559c6.7,-9.3,10,-15.3,10,-18z";
		case "rparen": return "M76,0c-16.7,0,-25,3,-25,9c0,2,2,6.3,6,13c21.3,28.7,42.3,60.3,\n63,95c96.7,156.7,172.8,332.5,228.5,527.5c55.7,195,92.8,416.5,111.5,664.5\nc11.3,139.3,17,290.7,17,454c0,28,1.7,43,3.3,45l0," + (t + 9) + "\nc-3,4,-3.3,16.7,-3.3,38c0,162,-5.7,313.7,-17,455c-18.7,248,-55.8,469.3,-111.5,664\nc-55.7,194.7,-131.8,370.3,-228.5,527c-20.7,34.7,-41.7,66.3,-63,95c-2,3.3,-4,7,-6,11\nc0,7.3,5.7,11,17,11c0,0,11,0,11,0c9.3,0,14.3,-0.3,15,-1c5.3,-5.3,10.3,-11,15,-17\nc242.7,-294.7,395.3,-681.7,458,-1161c21.3,-164.7,33.3,-350.7,36,-558\nl0,-" + (t + 144) + "c-2,-159.3,-10,-310.7,-24,-454c-53.3,-528,-210,-949.7,\n-470,-1265c-4.7,-6,-9.7,-11.7,-15,-17c-0.7,-0.7,-6.7,-1,-18,-1z";
		default: throw Error("Unknown stretchy delimiter.");
	}
};
function Lr(e) {
	return "toText" in e;
}
var Rr = class {
	constructor(e) {
		this.children = void 0, this.classes = void 0, this.height = void 0, this.depth = void 0, this.maxFontSize = void 0, this.style = void 0, this.children = e, this.classes = [], this.height = 0, this.depth = 0, this.maxFontSize = 0, this.style = {};
	}
	hasClass(e) {
		return this.classes.includes(e);
	}
	toNode() {
		for (var e = document.createDocumentFragment(), t = 0; t < this.children.length; t++) e.appendChild(this.children[t].toNode());
		return e;
	}
	toMarkup() {
		for (var e = "", t = 0; t < this.children.length; t++) e += this.children[t].toMarkup();
		return e;
	}
	toText() {
		return this.children.map((e) => {
			if (Lr(e)) return e.toText();
			throw Error("Expected MathDomNode with toText, got " + e.constructor.name);
		}).join("");
	}
}, zr = {
	pt: 1,
	mm: 7227 / 2540,
	cm: 7227 / 254,
	in: 72.27,
	bp: 803 / 800,
	pc: 12,
	dd: 1238 / 1157,
	cc: 14856 / 1157,
	nd: 685 / 642,
	nc: 1370 / 107,
	sp: 1 / 65536,
	px: 803 / 800
}, Br = {
	ex: !0,
	em: !0,
	mu: !0
}, Vr = function(e) {
	return typeof e != "string" && (e = e.unit), e in zr || e in Br || e === "ex";
}, Hr = function(e, t) {
	var n;
	if (e.unit in zr) n = zr[e.unit] / t.fontMetrics().ptPerEm / t.sizeMultiplier;
	else if (e.unit === "mu") n = t.fontMetrics().cssEmPerMu;
	else {
		var r = t.style.isTight() ? t.havingStyle(t.style.text()) : t;
		if (e.unit === "ex") n = r.fontMetrics().xHeight;
		else if (e.unit === "em") n = r.fontMetrics().quad;
		else throw new N("Invalid unit: '" + e.unit + "'");
		r !== t && (n *= r.sizeMultiplier / t.sizeMultiplier);
	}
	return Math.min(e.number * n, t.maxSize);
}, F = function(e) {
	return +e.toFixed(4) + "em";
}, Ur = function(e) {
	return e.filter((e) => e).join(" ");
}, Wr = function(e) {
	var t = "";
	for (var n of Object.keys(e)) {
		var r = e[n];
		r !== void 0 && (t += Gn(n) + ":" + r + ";");
	}
	return t;
}, Gr = function(e, t, n) {
	if (this.classes = e || [], this.attributes = {}, this.height = 0, this.depth = 0, this.maxFontSize = 0, this.style = n || {}, t) {
		t.style.isTight() && this.classes.push("mtight");
		var r = t.getColor();
		r && (this.style.color = r);
	}
}, Kr = function(e) {
	var t = document.createElement(e);
	t.className = Ur(this.classes), Object.assign(t.style, this.style);
	for (var n of Object.keys(this.attributes)) t.setAttribute(n, this.attributes[n]);
	for (var r = 0; r < this.children.length; r++) t.appendChild(this.children[r].toNode());
	return t;
}, qr = /[\s"'>/=\x00-\x1f]/, Jr = function(e) {
	var t = "<" + e;
	this.classes.length && (t += " class=\"" + Jn(Ur(this.classes)) + "\"");
	var n = Wr(this.style);
	n && (t += " style=\"" + Jn(n) + "\"");
	for (var r of Object.keys(this.attributes)) {
		if (qr.test(r)) throw new N("Invalid attribute name '" + r + "'");
		t += " " + r + "=\"" + Jn(this.attributes[r]) + "\"";
	}
	t += ">";
	for (var i = 0; i < this.children.length; i++) t += this.children[i].toMarkup();
	return t += "</" + e + ">", t;
}, Yr = class {
	constructor(e, t, n, r) {
		this.children = void 0, this.attributes = void 0, this.classes = void 0, this.height = void 0, this.depth = void 0, this.width = void 0, this.maxFontSize = void 0, this.style = void 0, this.italic = void 0, Gr.call(this, e, n, r), this.children = t || [];
	}
	setAttribute(e, t) {
		this.attributes[e] = t;
	}
	hasClass(e) {
		return this.classes.includes(e);
	}
	toNode() {
		return Kr.call(this, "span");
	}
	toMarkup() {
		return Jr.call(this, "span");
	}
}, Xr = class {
	constructor(e, t, n, r) {
		this.children = void 0, this.attributes = void 0, this.classes = void 0, this.height = void 0, this.depth = void 0, this.maxFontSize = void 0, this.style = void 0, Gr.call(this, t, r), this.children = n || [], this.setAttribute("href", e);
	}
	setAttribute(e, t) {
		this.attributes[e] = t;
	}
	hasClass(e) {
		return this.classes.includes(e);
	}
	toNode() {
		return Kr.call(this, "a");
	}
	toMarkup() {
		return Jr.call(this, "a");
	}
}, Zr = class {
	constructor(e, t, n) {
		this.src = void 0, this.alt = void 0, this.classes = void 0, this.height = void 0, this.depth = void 0, this.maxFontSize = void 0, this.style = void 0, this.alt = t, this.src = e, this.classes = ["mord"], this.height = 0, this.depth = 0, this.maxFontSize = 0, this.style = n;
	}
	hasClass(e) {
		return this.classes.includes(e);
	}
	toNode() {
		var e = document.createElement("img");
		return e.src = this.src, e.alt = this.alt, e.className = "mord", Object.assign(e.style, this.style), e;
	}
	toMarkup() {
		var e = "<img src=\"" + Jn(this.src) + "\"" + (" alt=\"" + Jn(this.alt) + "\""), t = Wr(this.style);
		return t && (e += " style=\"" + Jn(t) + "\""), e += "'/>", e;
	}
}, Qr = {
	î: "ı̂",
	ï: "ı̈",
	í: "ı́",
	ì: "ı̀"
}, $r = class {
	constructor(e, t, n, r, i, a, o, s) {
		this.text = void 0, this.height = void 0, this.depth = void 0, this.italic = void 0, this.skew = void 0, this.width = void 0, this.maxFontSize = void 0, this.classes = void 0, this.style = void 0, this.text = e, this.height = t || 0, this.depth = n || 0, this.italic = r || 0, this.skew = i || 0, this.width = a || 0, this.classes = o || [], this.style = s || {}, this.maxFontSize = 0;
		var c = xr(this.text.charCodeAt(0));
		c && this.classes.push(c + "_fallback"), /[îïíì]/.test(this.text) && (this.text = Qr[this.text]);
	}
	hasClass(e) {
		return this.classes.includes(e);
	}
	toNode() {
		var e = document.createTextNode(this.text), t = null;
		return this.italic > 0 && (t = document.createElement("span"), t.style.marginRight = F(this.italic)), this.classes.length > 0 && (t ||= document.createElement("span"), t.className = Ur(this.classes)), Object.keys(this.style).length > 0 && (t ||= document.createElement("span"), Object.assign(t.style, this.style)), t ? (t.appendChild(e), t) : e;
	}
	toMarkup() {
		var e = !1, t = "<span";
		this.classes.length && (e = !0, t += " class=\"", t += Jn(Ur(this.classes)), t += "\"");
		var n = "";
		this.italic > 0 && (n += "margin-right:" + F(this.italic) + ";"), n += Wr(this.style), n && (e = !0, t += " style=\"" + Jn(n) + "\"");
		var r = Jn(this.text);
		return e ? (t += ">", t += r, t += "</span>", t) : r;
	}
}, ei = class {
	constructor(e, t) {
		this.children = void 0, this.attributes = void 0, this.children = e || [], this.attributes = t || {};
	}
	toNode() {
		var e = document.createElementNS("http://www.w3.org/2000/svg", "svg");
		for (var t of Object.keys(this.attributes)) e.setAttribute(t, this.attributes[t]);
		for (var n = 0; n < this.children.length; n++) e.appendChild(this.children[n].toNode());
		return e;
	}
	toMarkup() {
		var e = "<svg xmlns=\"http://www.w3.org/2000/svg\"";
		for (var t of Object.keys(this.attributes)) e += " " + t + "=\"" + Jn(this.attributes[t]) + "\"";
		e += ">";
		for (var n = 0; n < this.children.length; n++) e += this.children[n].toMarkup();
		return e += "</svg>", e;
	}
}, ti = class {
	constructor(e, t) {
		this.pathName = void 0, this.alternate = void 0, this.pathName = e, this.alternate = t;
	}
	toNode() {
		var e = document.createElementNS("http://www.w3.org/2000/svg", "path");
		return this.alternate ? e.setAttribute("d", this.alternate) : e.setAttribute("d", Fr[this.pathName]), e;
	}
	toMarkup() {
		return this.alternate ? "<path d=\"" + Jn(this.alternate) + "\"/>" : "<path d=\"" + Jn(Fr[this.pathName]) + "\"/>";
	}
}, ni = class {
	constructor(e) {
		this.attributes = void 0, this.attributes = e || {};
	}
	toNode() {
		var e = document.createElementNS("http://www.w3.org/2000/svg", "line");
		for (var t of Object.keys(this.attributes)) e.setAttribute(t, this.attributes[t]);
		return e;
	}
	toMarkup() {
		var e = "<line";
		for (var t of Object.keys(this.attributes)) e += " " + t + "=\"" + Jn(this.attributes[t]) + "\"";
		return e += "/>", e;
	}
};
function ri(e) {
	if (e instanceof $r) return e;
	throw Error("Expected symbolNode but got " + String(e) + ".");
}
function ii(e) {
	if (e instanceof Yr) return e;
	throw Error("Expected span<HtmlDomNode> but got " + String(e) + ".");
}
var ai = (e) => e instanceof Yr || e instanceof Xr || e instanceof Rr, oi = {
	"AMS-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		65: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		66: [
			0,
			.68889,
			0,
			0,
			.66667
		],
		67: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		68: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		69: [
			0,
			.68889,
			0,
			0,
			.66667
		],
		70: [
			0,
			.68889,
			0,
			0,
			.61111
		],
		71: [
			0,
			.68889,
			0,
			0,
			.77778
		],
		72: [
			0,
			.68889,
			0,
			0,
			.77778
		],
		73: [
			0,
			.68889,
			0,
			0,
			.38889
		],
		74: [
			.16667,
			.68889,
			0,
			0,
			.5
		],
		75: [
			0,
			.68889,
			0,
			0,
			.77778
		],
		76: [
			0,
			.68889,
			0,
			0,
			.66667
		],
		77: [
			0,
			.68889,
			0,
			0,
			.94445
		],
		78: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		79: [
			.16667,
			.68889,
			0,
			0,
			.77778
		],
		80: [
			0,
			.68889,
			0,
			0,
			.61111
		],
		81: [
			.16667,
			.68889,
			0,
			0,
			.77778
		],
		82: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		83: [
			0,
			.68889,
			0,
			0,
			.55556
		],
		84: [
			0,
			.68889,
			0,
			0,
			.66667
		],
		85: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		86: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		87: [
			0,
			.68889,
			0,
			0,
			1
		],
		88: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		89: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		90: [
			0,
			.68889,
			0,
			0,
			.66667
		],
		107: [
			0,
			.68889,
			0,
			0,
			.55556
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		165: [
			0,
			.675,
			.025,
			0,
			.75
		],
		174: [
			.15559,
			.69224,
			0,
			0,
			.94666
		],
		240: [
			0,
			.68889,
			0,
			0,
			.55556
		],
		295: [
			0,
			.68889,
			0,
			0,
			.54028
		],
		710: [
			0,
			.825,
			0,
			0,
			2.33334
		],
		732: [
			0,
			.9,
			0,
			0,
			2.33334
		],
		770: [
			0,
			.825,
			0,
			0,
			2.33334
		],
		771: [
			0,
			.9,
			0,
			0,
			2.33334
		],
		989: [
			.08167,
			.58167,
			0,
			0,
			.77778
		],
		1008: [
			0,
			.43056,
			.04028,
			0,
			.66667
		],
		8245: [
			0,
			.54986,
			0,
			0,
			.275
		],
		8463: [
			0,
			.68889,
			0,
			0,
			.54028
		],
		8487: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		8498: [
			0,
			.68889,
			0,
			0,
			.55556
		],
		8502: [
			0,
			.68889,
			0,
			0,
			.66667
		],
		8503: [
			0,
			.68889,
			0,
			0,
			.44445
		],
		8504: [
			0,
			.68889,
			0,
			0,
			.66667
		],
		8513: [
			0,
			.68889,
			0,
			0,
			.63889
		],
		8592: [
			-.03598,
			.46402,
			0,
			0,
			.5
		],
		8594: [
			-.03598,
			.46402,
			0,
			0,
			.5
		],
		8602: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8603: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8606: [
			.01354,
			.52239,
			0,
			0,
			1
		],
		8608: [
			.01354,
			.52239,
			0,
			0,
			1
		],
		8610: [
			.01354,
			.52239,
			0,
			0,
			1.11111
		],
		8611: [
			.01354,
			.52239,
			0,
			0,
			1.11111
		],
		8619: [
			0,
			.54986,
			0,
			0,
			1
		],
		8620: [
			0,
			.54986,
			0,
			0,
			1
		],
		8621: [
			-.13313,
			.37788,
			0,
			0,
			1.38889
		],
		8622: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8624: [
			0,
			.69224,
			0,
			0,
			.5
		],
		8625: [
			0,
			.69224,
			0,
			0,
			.5
		],
		8630: [
			0,
			.43056,
			0,
			0,
			1
		],
		8631: [
			0,
			.43056,
			0,
			0,
			1
		],
		8634: [
			.08198,
			.58198,
			0,
			0,
			.77778
		],
		8635: [
			.08198,
			.58198,
			0,
			0,
			.77778
		],
		8638: [
			.19444,
			.69224,
			0,
			0,
			.41667
		],
		8639: [
			.19444,
			.69224,
			0,
			0,
			.41667
		],
		8642: [
			.19444,
			.69224,
			0,
			0,
			.41667
		],
		8643: [
			.19444,
			.69224,
			0,
			0,
			.41667
		],
		8644: [
			.1808,
			.675,
			0,
			0,
			1
		],
		8646: [
			.1808,
			.675,
			0,
			0,
			1
		],
		8647: [
			.1808,
			.675,
			0,
			0,
			1
		],
		8648: [
			.19444,
			.69224,
			0,
			0,
			.83334
		],
		8649: [
			.1808,
			.675,
			0,
			0,
			1
		],
		8650: [
			.19444,
			.69224,
			0,
			0,
			.83334
		],
		8651: [
			.01354,
			.52239,
			0,
			0,
			1
		],
		8652: [
			.01354,
			.52239,
			0,
			0,
			1
		],
		8653: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8654: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8655: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8666: [
			.13667,
			.63667,
			0,
			0,
			1
		],
		8667: [
			.13667,
			.63667,
			0,
			0,
			1
		],
		8669: [
			-.13313,
			.37788,
			0,
			0,
			1
		],
		8672: [
			-.064,
			.437,
			0,
			0,
			1.334
		],
		8674: [
			-.064,
			.437,
			0,
			0,
			1.334
		],
		8705: [
			0,
			.825,
			0,
			0,
			.5
		],
		8708: [
			0,
			.68889,
			0,
			0,
			.55556
		],
		8709: [
			.08167,
			.58167,
			0,
			0,
			.77778
		],
		8717: [
			0,
			.43056,
			0,
			0,
			.42917
		],
		8722: [
			-.03598,
			.46402,
			0,
			0,
			.5
		],
		8724: [
			.08198,
			.69224,
			0,
			0,
			.77778
		],
		8726: [
			.08167,
			.58167,
			0,
			0,
			.77778
		],
		8733: [
			0,
			.69224,
			0,
			0,
			.77778
		],
		8736: [
			0,
			.69224,
			0,
			0,
			.72222
		],
		8737: [
			0,
			.69224,
			0,
			0,
			.72222
		],
		8738: [
			.03517,
			.52239,
			0,
			0,
			.72222
		],
		8739: [
			.08167,
			.58167,
			0,
			0,
			.22222
		],
		8740: [
			.25142,
			.74111,
			0,
			0,
			.27778
		],
		8741: [
			.08167,
			.58167,
			0,
			0,
			.38889
		],
		8742: [
			.25142,
			.74111,
			0,
			0,
			.5
		],
		8756: [
			0,
			.69224,
			0,
			0,
			.66667
		],
		8757: [
			0,
			.69224,
			0,
			0,
			.66667
		],
		8764: [
			-.13313,
			.36687,
			0,
			0,
			.77778
		],
		8765: [
			-.13313,
			.37788,
			0,
			0,
			.77778
		],
		8769: [
			-.13313,
			.36687,
			0,
			0,
			.77778
		],
		8770: [
			-.03625,
			.46375,
			0,
			0,
			.77778
		],
		8774: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8776: [
			-.01688,
			.48312,
			0,
			0,
			.77778
		],
		8778: [
			.08167,
			.58167,
			0,
			0,
			.77778
		],
		8782: [
			.06062,
			.54986,
			0,
			0,
			.77778
		],
		8783: [
			.06062,
			.54986,
			0,
			0,
			.77778
		],
		8785: [
			.08198,
			.58198,
			0,
			0,
			.77778
		],
		8786: [
			.08198,
			.58198,
			0,
			0,
			.77778
		],
		8787: [
			.08198,
			.58198,
			0,
			0,
			.77778
		],
		8790: [
			0,
			.69224,
			0,
			0,
			.77778
		],
		8791: [
			.22958,
			.72958,
			0,
			0,
			.77778
		],
		8796: [
			.08198,
			.91667,
			0,
			0,
			.77778
		],
		8806: [
			.25583,
			.75583,
			0,
			0,
			.77778
		],
		8807: [
			.25583,
			.75583,
			0,
			0,
			.77778
		],
		8808: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		8809: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		8812: [
			.25583,
			.75583,
			0,
			0,
			.5
		],
		8814: [
			.20576,
			.70576,
			0,
			0,
			.77778
		],
		8815: [
			.20576,
			.70576,
			0,
			0,
			.77778
		],
		8816: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8817: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8818: [
			.22958,
			.72958,
			0,
			0,
			.77778
		],
		8819: [
			.22958,
			.72958,
			0,
			0,
			.77778
		],
		8822: [
			.1808,
			.675,
			0,
			0,
			.77778
		],
		8823: [
			.1808,
			.675,
			0,
			0,
			.77778
		],
		8828: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		8829: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		8830: [
			.22958,
			.72958,
			0,
			0,
			.77778
		],
		8831: [
			.22958,
			.72958,
			0,
			0,
			.77778
		],
		8832: [
			.20576,
			.70576,
			0,
			0,
			.77778
		],
		8833: [
			.20576,
			.70576,
			0,
			0,
			.77778
		],
		8840: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8841: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8842: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		8843: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		8847: [
			.03517,
			.54986,
			0,
			0,
			.77778
		],
		8848: [
			.03517,
			.54986,
			0,
			0,
			.77778
		],
		8858: [
			.08198,
			.58198,
			0,
			0,
			.77778
		],
		8859: [
			.08198,
			.58198,
			0,
			0,
			.77778
		],
		8861: [
			.08198,
			.58198,
			0,
			0,
			.77778
		],
		8862: [
			0,
			.675,
			0,
			0,
			.77778
		],
		8863: [
			0,
			.675,
			0,
			0,
			.77778
		],
		8864: [
			0,
			.675,
			0,
			0,
			.77778
		],
		8865: [
			0,
			.675,
			0,
			0,
			.77778
		],
		8872: [
			0,
			.69224,
			0,
			0,
			.61111
		],
		8873: [
			0,
			.69224,
			0,
			0,
			.72222
		],
		8874: [
			0,
			.69224,
			0,
			0,
			.88889
		],
		8876: [
			0,
			.68889,
			0,
			0,
			.61111
		],
		8877: [
			0,
			.68889,
			0,
			0,
			.61111
		],
		8878: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		8879: [
			0,
			.68889,
			0,
			0,
			.72222
		],
		8882: [
			.03517,
			.54986,
			0,
			0,
			.77778
		],
		8883: [
			.03517,
			.54986,
			0,
			0,
			.77778
		],
		8884: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		8885: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		8888: [
			0,
			.54986,
			0,
			0,
			1.11111
		],
		8890: [
			.19444,
			.43056,
			0,
			0,
			.55556
		],
		8891: [
			.19444,
			.69224,
			0,
			0,
			.61111
		],
		8892: [
			.19444,
			.69224,
			0,
			0,
			.61111
		],
		8901: [
			0,
			.54986,
			0,
			0,
			.27778
		],
		8903: [
			.08167,
			.58167,
			0,
			0,
			.77778
		],
		8905: [
			.08167,
			.58167,
			0,
			0,
			.77778
		],
		8906: [
			.08167,
			.58167,
			0,
			0,
			.77778
		],
		8907: [
			0,
			.69224,
			0,
			0,
			.77778
		],
		8908: [
			0,
			.69224,
			0,
			0,
			.77778
		],
		8909: [
			-.03598,
			.46402,
			0,
			0,
			.77778
		],
		8910: [
			0,
			.54986,
			0,
			0,
			.76042
		],
		8911: [
			0,
			.54986,
			0,
			0,
			.76042
		],
		8912: [
			.03517,
			.54986,
			0,
			0,
			.77778
		],
		8913: [
			.03517,
			.54986,
			0,
			0,
			.77778
		],
		8914: [
			0,
			.54986,
			0,
			0,
			.66667
		],
		8915: [
			0,
			.54986,
			0,
			0,
			.66667
		],
		8916: [
			0,
			.69224,
			0,
			0,
			.66667
		],
		8918: [
			.0391,
			.5391,
			0,
			0,
			.77778
		],
		8919: [
			.0391,
			.5391,
			0,
			0,
			.77778
		],
		8920: [
			.03517,
			.54986,
			0,
			0,
			1.33334
		],
		8921: [
			.03517,
			.54986,
			0,
			0,
			1.33334
		],
		8922: [
			.38569,
			.88569,
			0,
			0,
			.77778
		],
		8923: [
			.38569,
			.88569,
			0,
			0,
			.77778
		],
		8926: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		8927: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		8928: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8929: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8934: [
			.23222,
			.74111,
			0,
			0,
			.77778
		],
		8935: [
			.23222,
			.74111,
			0,
			0,
			.77778
		],
		8936: [
			.23222,
			.74111,
			0,
			0,
			.77778
		],
		8937: [
			.23222,
			.74111,
			0,
			0,
			.77778
		],
		8938: [
			.20576,
			.70576,
			0,
			0,
			.77778
		],
		8939: [
			.20576,
			.70576,
			0,
			0,
			.77778
		],
		8940: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8941: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		8994: [
			.19444,
			.69224,
			0,
			0,
			.77778
		],
		8995: [
			.19444,
			.69224,
			0,
			0,
			.77778
		],
		9416: [
			.15559,
			.69224,
			0,
			0,
			.90222
		],
		9484: [
			0,
			.69224,
			0,
			0,
			.5
		],
		9488: [
			0,
			.69224,
			0,
			0,
			.5
		],
		9492: [
			0,
			.37788,
			0,
			0,
			.5
		],
		9496: [
			0,
			.37788,
			0,
			0,
			.5
		],
		9585: [
			.19444,
			.68889,
			0,
			0,
			.88889
		],
		9586: [
			.19444,
			.74111,
			0,
			0,
			.88889
		],
		9632: [
			0,
			.675,
			0,
			0,
			.77778
		],
		9633: [
			0,
			.675,
			0,
			0,
			.77778
		],
		9650: [
			0,
			.54986,
			0,
			0,
			.72222
		],
		9651: [
			0,
			.54986,
			0,
			0,
			.72222
		],
		9654: [
			.03517,
			.54986,
			0,
			0,
			.77778
		],
		9660: [
			0,
			.54986,
			0,
			0,
			.72222
		],
		9661: [
			0,
			.54986,
			0,
			0,
			.72222
		],
		9664: [
			.03517,
			.54986,
			0,
			0,
			.77778
		],
		9674: [
			.11111,
			.69224,
			0,
			0,
			.66667
		],
		9733: [
			.19444,
			.69224,
			0,
			0,
			.94445
		],
		10003: [
			0,
			.69224,
			0,
			0,
			.83334
		],
		10016: [
			0,
			.69224,
			0,
			0,
			.83334
		],
		10731: [
			.11111,
			.69224,
			0,
			0,
			.66667
		],
		10846: [
			.19444,
			.75583,
			0,
			0,
			.61111
		],
		10877: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		10878: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		10885: [
			.25583,
			.75583,
			0,
			0,
			.77778
		],
		10886: [
			.25583,
			.75583,
			0,
			0,
			.77778
		],
		10887: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		10888: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		10889: [
			.26167,
			.75726,
			0,
			0,
			.77778
		],
		10890: [
			.26167,
			.75726,
			0,
			0,
			.77778
		],
		10891: [
			.48256,
			.98256,
			0,
			0,
			.77778
		],
		10892: [
			.48256,
			.98256,
			0,
			0,
			.77778
		],
		10901: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		10902: [
			.13667,
			.63667,
			0,
			0,
			.77778
		],
		10933: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		10934: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		10935: [
			.26167,
			.75726,
			0,
			0,
			.77778
		],
		10936: [
			.26167,
			.75726,
			0,
			0,
			.77778
		],
		10937: [
			.26167,
			.75726,
			0,
			0,
			.77778
		],
		10938: [
			.26167,
			.75726,
			0,
			0,
			.77778
		],
		10949: [
			.25583,
			.75583,
			0,
			0,
			.77778
		],
		10950: [
			.25583,
			.75583,
			0,
			0,
			.77778
		],
		10955: [
			.28481,
			.79383,
			0,
			0,
			.77778
		],
		10956: [
			.28481,
			.79383,
			0,
			0,
			.77778
		],
		57350: [
			.08167,
			.58167,
			0,
			0,
			.22222
		],
		57351: [
			.08167,
			.58167,
			0,
			0,
			.38889
		],
		57352: [
			.08167,
			.58167,
			0,
			0,
			.77778
		],
		57353: [
			0,
			.43056,
			.04028,
			0,
			.66667
		],
		57356: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		57357: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		57358: [
			.41951,
			.91951,
			0,
			0,
			.77778
		],
		57359: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		57360: [
			.30274,
			.79383,
			0,
			0,
			.77778
		],
		57361: [
			.41951,
			.91951,
			0,
			0,
			.77778
		],
		57366: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		57367: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		57368: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		57369: [
			.25142,
			.75726,
			0,
			0,
			.77778
		],
		57370: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		57371: [
			.13597,
			.63597,
			0,
			0,
			.77778
		]
	},
	"Caligraphic-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		65: [
			0,
			.68333,
			0,
			.19445,
			.79847
		],
		66: [
			0,
			.68333,
			.03041,
			.13889,
			.65681
		],
		67: [
			0,
			.68333,
			.05834,
			.13889,
			.52653
		],
		68: [
			0,
			.68333,
			.02778,
			.08334,
			.77139
		],
		69: [
			0,
			.68333,
			.08944,
			.11111,
			.52778
		],
		70: [
			0,
			.68333,
			.09931,
			.11111,
			.71875
		],
		71: [
			.09722,
			.68333,
			.0593,
			.11111,
			.59487
		],
		72: [
			0,
			.68333,
			.00965,
			.11111,
			.84452
		],
		73: [
			0,
			.68333,
			.07382,
			0,
			.54452
		],
		74: [
			.09722,
			.68333,
			.18472,
			.16667,
			.67778
		],
		75: [
			0,
			.68333,
			.01445,
			.05556,
			.76195
		],
		76: [
			0,
			.68333,
			0,
			.13889,
			.68972
		],
		77: [
			0,
			.68333,
			0,
			.13889,
			1.2009
		],
		78: [
			0,
			.68333,
			.14736,
			.08334,
			.82049
		],
		79: [
			0,
			.68333,
			.02778,
			.11111,
			.79611
		],
		80: [
			0,
			.68333,
			.08222,
			.08334,
			.69556
		],
		81: [
			.09722,
			.68333,
			0,
			.11111,
			.81667
		],
		82: [
			0,
			.68333,
			0,
			.08334,
			.8475
		],
		83: [
			0,
			.68333,
			.075,
			.13889,
			.60556
		],
		84: [
			0,
			.68333,
			.25417,
			0,
			.54464
		],
		85: [
			0,
			.68333,
			.09931,
			.08334,
			.62583
		],
		86: [
			0,
			.68333,
			.08222,
			0,
			.61278
		],
		87: [
			0,
			.68333,
			.08222,
			.08334,
			.98778
		],
		88: [
			0,
			.68333,
			.14643,
			.13889,
			.7133
		],
		89: [
			.09722,
			.68333,
			.08222,
			.08334,
			.66834
		],
		90: [
			0,
			.68333,
			.07944,
			.13889,
			.72473
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		]
	},
	"Fraktur-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		33: [
			0,
			.69141,
			0,
			0,
			.29574
		],
		34: [
			0,
			.69141,
			0,
			0,
			.21471
		],
		38: [
			0,
			.69141,
			0,
			0,
			.73786
		],
		39: [
			0,
			.69141,
			0,
			0,
			.21201
		],
		40: [
			.24982,
			.74947,
			0,
			0,
			.38865
		],
		41: [
			.24982,
			.74947,
			0,
			0,
			.38865
		],
		42: [
			0,
			.62119,
			0,
			0,
			.27764
		],
		43: [
			.08319,
			.58283,
			0,
			0,
			.75623
		],
		44: [
			0,
			.10803,
			0,
			0,
			.27764
		],
		45: [
			.08319,
			.58283,
			0,
			0,
			.75623
		],
		46: [
			0,
			.10803,
			0,
			0,
			.27764
		],
		47: [
			.24982,
			.74947,
			0,
			0,
			.50181
		],
		48: [
			0,
			.47534,
			0,
			0,
			.50181
		],
		49: [
			0,
			.47534,
			0,
			0,
			.50181
		],
		50: [
			0,
			.47534,
			0,
			0,
			.50181
		],
		51: [
			.18906,
			.47534,
			0,
			0,
			.50181
		],
		52: [
			.18906,
			.47534,
			0,
			0,
			.50181
		],
		53: [
			.18906,
			.47534,
			0,
			0,
			.50181
		],
		54: [
			0,
			.69141,
			0,
			0,
			.50181
		],
		55: [
			.18906,
			.47534,
			0,
			0,
			.50181
		],
		56: [
			0,
			.69141,
			0,
			0,
			.50181
		],
		57: [
			.18906,
			.47534,
			0,
			0,
			.50181
		],
		58: [
			0,
			.47534,
			0,
			0,
			.21606
		],
		59: [
			.12604,
			.47534,
			0,
			0,
			.21606
		],
		61: [
			-.13099,
			.36866,
			0,
			0,
			.75623
		],
		63: [
			0,
			.69141,
			0,
			0,
			.36245
		],
		65: [
			0,
			.69141,
			0,
			0,
			.7176
		],
		66: [
			0,
			.69141,
			0,
			0,
			.88397
		],
		67: [
			0,
			.69141,
			0,
			0,
			.61254
		],
		68: [
			0,
			.69141,
			0,
			0,
			.83158
		],
		69: [
			0,
			.69141,
			0,
			0,
			.66278
		],
		70: [
			.12604,
			.69141,
			0,
			0,
			.61119
		],
		71: [
			0,
			.69141,
			0,
			0,
			.78539
		],
		72: [
			.06302,
			.69141,
			0,
			0,
			.7203
		],
		73: [
			0,
			.69141,
			0,
			0,
			.55448
		],
		74: [
			.12604,
			.69141,
			0,
			0,
			.55231
		],
		75: [
			0,
			.69141,
			0,
			0,
			.66845
		],
		76: [
			0,
			.69141,
			0,
			0,
			.66602
		],
		77: [
			0,
			.69141,
			0,
			0,
			1.04953
		],
		78: [
			0,
			.69141,
			0,
			0,
			.83212
		],
		79: [
			0,
			.69141,
			0,
			0,
			.82699
		],
		80: [
			.18906,
			.69141,
			0,
			0,
			.82753
		],
		81: [
			.03781,
			.69141,
			0,
			0,
			.82699
		],
		82: [
			0,
			.69141,
			0,
			0,
			.82807
		],
		83: [
			0,
			.69141,
			0,
			0,
			.82861
		],
		84: [
			0,
			.69141,
			0,
			0,
			.66899
		],
		85: [
			0,
			.69141,
			0,
			0,
			.64576
		],
		86: [
			0,
			.69141,
			0,
			0,
			.83131
		],
		87: [
			0,
			.69141,
			0,
			0,
			1.04602
		],
		88: [
			0,
			.69141,
			0,
			0,
			.71922
		],
		89: [
			.18906,
			.69141,
			0,
			0,
			.83293
		],
		90: [
			.12604,
			.69141,
			0,
			0,
			.60201
		],
		91: [
			.24982,
			.74947,
			0,
			0,
			.27764
		],
		93: [
			.24982,
			.74947,
			0,
			0,
			.27764
		],
		94: [
			0,
			.69141,
			0,
			0,
			.49965
		],
		97: [
			0,
			.47534,
			0,
			0,
			.50046
		],
		98: [
			0,
			.69141,
			0,
			0,
			.51315
		],
		99: [
			0,
			.47534,
			0,
			0,
			.38946
		],
		100: [
			0,
			.62119,
			0,
			0,
			.49857
		],
		101: [
			0,
			.47534,
			0,
			0,
			.40053
		],
		102: [
			.18906,
			.69141,
			0,
			0,
			.32626
		],
		103: [
			.18906,
			.47534,
			0,
			0,
			.5037
		],
		104: [
			.18906,
			.69141,
			0,
			0,
			.52126
		],
		105: [
			0,
			.69141,
			0,
			0,
			.27899
		],
		106: [
			0,
			.69141,
			0,
			0,
			.28088
		],
		107: [
			0,
			.69141,
			0,
			0,
			.38946
		],
		108: [
			0,
			.69141,
			0,
			0,
			.27953
		],
		109: [
			0,
			.47534,
			0,
			0,
			.76676
		],
		110: [
			0,
			.47534,
			0,
			0,
			.52666
		],
		111: [
			0,
			.47534,
			0,
			0,
			.48885
		],
		112: [
			.18906,
			.52396,
			0,
			0,
			.50046
		],
		113: [
			.18906,
			.47534,
			0,
			0,
			.48912
		],
		114: [
			0,
			.47534,
			0,
			0,
			.38919
		],
		115: [
			0,
			.47534,
			0,
			0,
			.44266
		],
		116: [
			0,
			.62119,
			0,
			0,
			.33301
		],
		117: [
			0,
			.47534,
			0,
			0,
			.5172
		],
		118: [
			0,
			.52396,
			0,
			0,
			.5118
		],
		119: [
			0,
			.52396,
			0,
			0,
			.77351
		],
		120: [
			.18906,
			.47534,
			0,
			0,
			.38865
		],
		121: [
			.18906,
			.47534,
			0,
			0,
			.49884
		],
		122: [
			.18906,
			.47534,
			0,
			0,
			.39054
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		8216: [
			0,
			.69141,
			0,
			0,
			.21471
		],
		8217: [
			0,
			.69141,
			0,
			0,
			.21471
		],
		58112: [
			0,
			.62119,
			0,
			0,
			.49749
		],
		58113: [
			0,
			.62119,
			0,
			0,
			.4983
		],
		58114: [
			.18906,
			.69141,
			0,
			0,
			.33328
		],
		58115: [
			.18906,
			.69141,
			0,
			0,
			.32923
		],
		58116: [
			.18906,
			.47534,
			0,
			0,
			.50343
		],
		58117: [
			0,
			.69141,
			0,
			0,
			.33301
		],
		58118: [
			0,
			.62119,
			0,
			0,
			.33409
		],
		58119: [
			0,
			.47534,
			0,
			0,
			.50073
		]
	},
	"Main-Bold": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		33: [
			0,
			.69444,
			0,
			0,
			.35
		],
		34: [
			0,
			.69444,
			0,
			0,
			.60278
		],
		35: [
			.19444,
			.69444,
			0,
			0,
			.95833
		],
		36: [
			.05556,
			.75,
			0,
			0,
			.575
		],
		37: [
			.05556,
			.75,
			0,
			0,
			.95833
		],
		38: [
			0,
			.69444,
			0,
			0,
			.89444
		],
		39: [
			0,
			.69444,
			0,
			0,
			.31944
		],
		40: [
			.25,
			.75,
			0,
			0,
			.44722
		],
		41: [
			.25,
			.75,
			0,
			0,
			.44722
		],
		42: [
			0,
			.75,
			0,
			0,
			.575
		],
		43: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		44: [
			.19444,
			.15556,
			0,
			0,
			.31944
		],
		45: [
			0,
			.44444,
			0,
			0,
			.38333
		],
		46: [
			0,
			.15556,
			0,
			0,
			.31944
		],
		47: [
			.25,
			.75,
			0,
			0,
			.575
		],
		48: [
			0,
			.64444,
			0,
			0,
			.575
		],
		49: [
			0,
			.64444,
			0,
			0,
			.575
		],
		50: [
			0,
			.64444,
			0,
			0,
			.575
		],
		51: [
			0,
			.64444,
			0,
			0,
			.575
		],
		52: [
			0,
			.64444,
			0,
			0,
			.575
		],
		53: [
			0,
			.64444,
			0,
			0,
			.575
		],
		54: [
			0,
			.64444,
			0,
			0,
			.575
		],
		55: [
			0,
			.64444,
			0,
			0,
			.575
		],
		56: [
			0,
			.64444,
			0,
			0,
			.575
		],
		57: [
			0,
			.64444,
			0,
			0,
			.575
		],
		58: [
			0,
			.44444,
			0,
			0,
			.31944
		],
		59: [
			.19444,
			.44444,
			0,
			0,
			.31944
		],
		60: [
			.08556,
			.58556,
			0,
			0,
			.89444
		],
		61: [
			-.10889,
			.39111,
			0,
			0,
			.89444
		],
		62: [
			.08556,
			.58556,
			0,
			0,
			.89444
		],
		63: [
			0,
			.69444,
			0,
			0,
			.54305
		],
		64: [
			0,
			.69444,
			0,
			0,
			.89444
		],
		65: [
			0,
			.68611,
			0,
			0,
			.86944
		],
		66: [
			0,
			.68611,
			0,
			0,
			.81805
		],
		67: [
			0,
			.68611,
			0,
			0,
			.83055
		],
		68: [
			0,
			.68611,
			0,
			0,
			.88194
		],
		69: [
			0,
			.68611,
			0,
			0,
			.75555
		],
		70: [
			0,
			.68611,
			0,
			0,
			.72361
		],
		71: [
			0,
			.68611,
			0,
			0,
			.90416
		],
		72: [
			0,
			.68611,
			0,
			0,
			.9
		],
		73: [
			0,
			.68611,
			0,
			0,
			.43611
		],
		74: [
			0,
			.68611,
			0,
			0,
			.59444
		],
		75: [
			0,
			.68611,
			0,
			0,
			.90138
		],
		76: [
			0,
			.68611,
			0,
			0,
			.69166
		],
		77: [
			0,
			.68611,
			0,
			0,
			1.09166
		],
		78: [
			0,
			.68611,
			0,
			0,
			.9
		],
		79: [
			0,
			.68611,
			0,
			0,
			.86388
		],
		80: [
			0,
			.68611,
			0,
			0,
			.78611
		],
		81: [
			.19444,
			.68611,
			0,
			0,
			.86388
		],
		82: [
			0,
			.68611,
			0,
			0,
			.8625
		],
		83: [
			0,
			.68611,
			0,
			0,
			.63889
		],
		84: [
			0,
			.68611,
			0,
			0,
			.8
		],
		85: [
			0,
			.68611,
			0,
			0,
			.88472
		],
		86: [
			0,
			.68611,
			.01597,
			0,
			.86944
		],
		87: [
			0,
			.68611,
			.01597,
			0,
			1.18888
		],
		88: [
			0,
			.68611,
			0,
			0,
			.86944
		],
		89: [
			0,
			.68611,
			.02875,
			0,
			.86944
		],
		90: [
			0,
			.68611,
			0,
			0,
			.70277
		],
		91: [
			.25,
			.75,
			0,
			0,
			.31944
		],
		92: [
			.25,
			.75,
			0,
			0,
			.575
		],
		93: [
			.25,
			.75,
			0,
			0,
			.31944
		],
		94: [
			0,
			.69444,
			0,
			0,
			.575
		],
		95: [
			.31,
			.13444,
			.03194,
			0,
			.575
		],
		97: [
			0,
			.44444,
			0,
			0,
			.55902
		],
		98: [
			0,
			.69444,
			0,
			0,
			.63889
		],
		99: [
			0,
			.44444,
			0,
			0,
			.51111
		],
		100: [
			0,
			.69444,
			0,
			0,
			.63889
		],
		101: [
			0,
			.44444,
			0,
			0,
			.52708
		],
		102: [
			0,
			.69444,
			.10903,
			0,
			.35139
		],
		103: [
			.19444,
			.44444,
			.01597,
			0,
			.575
		],
		104: [
			0,
			.69444,
			0,
			0,
			.63889
		],
		105: [
			0,
			.69444,
			0,
			0,
			.31944
		],
		106: [
			.19444,
			.69444,
			0,
			0,
			.35139
		],
		107: [
			0,
			.69444,
			0,
			0,
			.60694
		],
		108: [
			0,
			.69444,
			0,
			0,
			.31944
		],
		109: [
			0,
			.44444,
			0,
			0,
			.95833
		],
		110: [
			0,
			.44444,
			0,
			0,
			.63889
		],
		111: [
			0,
			.44444,
			0,
			0,
			.575
		],
		112: [
			.19444,
			.44444,
			0,
			0,
			.63889
		],
		113: [
			.19444,
			.44444,
			0,
			0,
			.60694
		],
		114: [
			0,
			.44444,
			0,
			0,
			.47361
		],
		115: [
			0,
			.44444,
			0,
			0,
			.45361
		],
		116: [
			0,
			.63492,
			0,
			0,
			.44722
		],
		117: [
			0,
			.44444,
			0,
			0,
			.63889
		],
		118: [
			0,
			.44444,
			.01597,
			0,
			.60694
		],
		119: [
			0,
			.44444,
			.01597,
			0,
			.83055
		],
		120: [
			0,
			.44444,
			0,
			0,
			.60694
		],
		121: [
			.19444,
			.44444,
			.01597,
			0,
			.60694
		],
		122: [
			0,
			.44444,
			0,
			0,
			.51111
		],
		123: [
			.25,
			.75,
			0,
			0,
			.575
		],
		124: [
			.25,
			.75,
			0,
			0,
			.31944
		],
		125: [
			.25,
			.75,
			0,
			0,
			.575
		],
		126: [
			.35,
			.34444,
			0,
			0,
			.575
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		163: [
			0,
			.69444,
			0,
			0,
			.86853
		],
		168: [
			0,
			.69444,
			0,
			0,
			.575
		],
		172: [
			0,
			.44444,
			0,
			0,
			.76666
		],
		176: [
			0,
			.69444,
			0,
			0,
			.86944
		],
		177: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		184: [
			.17014,
			0,
			0,
			0,
			.51111
		],
		198: [
			0,
			.68611,
			0,
			0,
			1.04166
		],
		215: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		216: [
			.04861,
			.73472,
			0,
			0,
			.89444
		],
		223: [
			0,
			.69444,
			0,
			0,
			.59722
		],
		230: [
			0,
			.44444,
			0,
			0,
			.83055
		],
		247: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		248: [
			.09722,
			.54167,
			0,
			0,
			.575
		],
		305: [
			0,
			.44444,
			0,
			0,
			.31944
		],
		338: [
			0,
			.68611,
			0,
			0,
			1.16944
		],
		339: [
			0,
			.44444,
			0,
			0,
			.89444
		],
		567: [
			.19444,
			.44444,
			0,
			0,
			.35139
		],
		710: [
			0,
			.69444,
			0,
			0,
			.575
		],
		711: [
			0,
			.63194,
			0,
			0,
			.575
		],
		713: [
			0,
			.59611,
			0,
			0,
			.575
		],
		714: [
			0,
			.69444,
			0,
			0,
			.575
		],
		715: [
			0,
			.69444,
			0,
			0,
			.575
		],
		728: [
			0,
			.69444,
			0,
			0,
			.575
		],
		729: [
			0,
			.69444,
			0,
			0,
			.31944
		],
		730: [
			0,
			.69444,
			0,
			0,
			.86944
		],
		732: [
			0,
			.69444,
			0,
			0,
			.575
		],
		733: [
			0,
			.69444,
			0,
			0,
			.575
		],
		915: [
			0,
			.68611,
			0,
			0,
			.69166
		],
		916: [
			0,
			.68611,
			0,
			0,
			.95833
		],
		920: [
			0,
			.68611,
			0,
			0,
			.89444
		],
		923: [
			0,
			.68611,
			0,
			0,
			.80555
		],
		926: [
			0,
			.68611,
			0,
			0,
			.76666
		],
		928: [
			0,
			.68611,
			0,
			0,
			.9
		],
		931: [
			0,
			.68611,
			0,
			0,
			.83055
		],
		933: [
			0,
			.68611,
			0,
			0,
			.89444
		],
		934: [
			0,
			.68611,
			0,
			0,
			.83055
		],
		936: [
			0,
			.68611,
			0,
			0,
			.89444
		],
		937: [
			0,
			.68611,
			0,
			0,
			.83055
		],
		8211: [
			0,
			.44444,
			.03194,
			0,
			.575
		],
		8212: [
			0,
			.44444,
			.03194,
			0,
			1.14999
		],
		8216: [
			0,
			.69444,
			0,
			0,
			.31944
		],
		8217: [
			0,
			.69444,
			0,
			0,
			.31944
		],
		8220: [
			0,
			.69444,
			0,
			0,
			.60278
		],
		8221: [
			0,
			.69444,
			0,
			0,
			.60278
		],
		8224: [
			.19444,
			.69444,
			0,
			0,
			.51111
		],
		8225: [
			.19444,
			.69444,
			0,
			0,
			.51111
		],
		8242: [
			0,
			.55556,
			0,
			0,
			.34444
		],
		8407: [
			0,
			.72444,
			.15486,
			0,
			.575
		],
		8463: [
			0,
			.69444,
			0,
			0,
			.66759
		],
		8465: [
			0,
			.69444,
			0,
			0,
			.83055
		],
		8467: [
			0,
			.69444,
			0,
			0,
			.47361
		],
		8472: [
			.19444,
			.44444,
			0,
			0,
			.74027
		],
		8476: [
			0,
			.69444,
			0,
			0,
			.83055
		],
		8501: [
			0,
			.69444,
			0,
			0,
			.70277
		],
		8592: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8593: [
			.19444,
			.69444,
			0,
			0,
			.575
		],
		8594: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8595: [
			.19444,
			.69444,
			0,
			0,
			.575
		],
		8596: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8597: [
			.25,
			.75,
			0,
			0,
			.575
		],
		8598: [
			.19444,
			.69444,
			0,
			0,
			1.14999
		],
		8599: [
			.19444,
			.69444,
			0,
			0,
			1.14999
		],
		8600: [
			.19444,
			.69444,
			0,
			0,
			1.14999
		],
		8601: [
			.19444,
			.69444,
			0,
			0,
			1.14999
		],
		8636: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8637: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8640: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8641: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8656: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8657: [
			.19444,
			.69444,
			0,
			0,
			.70277
		],
		8658: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8659: [
			.19444,
			.69444,
			0,
			0,
			.70277
		],
		8660: [
			-.10889,
			.39111,
			0,
			0,
			1.14999
		],
		8661: [
			.25,
			.75,
			0,
			0,
			.70277
		],
		8704: [
			0,
			.69444,
			0,
			0,
			.63889
		],
		8706: [
			0,
			.69444,
			.06389,
			0,
			.62847
		],
		8707: [
			0,
			.69444,
			0,
			0,
			.63889
		],
		8709: [
			.05556,
			.75,
			0,
			0,
			.575
		],
		8711: [
			0,
			.68611,
			0,
			0,
			.95833
		],
		8712: [
			.08556,
			.58556,
			0,
			0,
			.76666
		],
		8715: [
			.08556,
			.58556,
			0,
			0,
			.76666
		],
		8722: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		8723: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		8725: [
			.25,
			.75,
			0,
			0,
			.575
		],
		8726: [
			.25,
			.75,
			0,
			0,
			.575
		],
		8727: [
			-.02778,
			.47222,
			0,
			0,
			.575
		],
		8728: [
			-.02639,
			.47361,
			0,
			0,
			.575
		],
		8729: [
			-.02639,
			.47361,
			0,
			0,
			.575
		],
		8730: [
			.18,
			.82,
			0,
			0,
			.95833
		],
		8733: [
			0,
			.44444,
			0,
			0,
			.89444
		],
		8734: [
			0,
			.44444,
			0,
			0,
			1.14999
		],
		8736: [
			0,
			.69224,
			0,
			0,
			.72222
		],
		8739: [
			.25,
			.75,
			0,
			0,
			.31944
		],
		8741: [
			.25,
			.75,
			0,
			0,
			.575
		],
		8743: [
			0,
			.55556,
			0,
			0,
			.76666
		],
		8744: [
			0,
			.55556,
			0,
			0,
			.76666
		],
		8745: [
			0,
			.55556,
			0,
			0,
			.76666
		],
		8746: [
			0,
			.55556,
			0,
			0,
			.76666
		],
		8747: [
			.19444,
			.69444,
			.12778,
			0,
			.56875
		],
		8764: [
			-.10889,
			.39111,
			0,
			0,
			.89444
		],
		8768: [
			.19444,
			.69444,
			0,
			0,
			.31944
		],
		8771: [
			.00222,
			.50222,
			0,
			0,
			.89444
		],
		8773: [
			.027,
			.638,
			0,
			0,
			.894
		],
		8776: [
			.02444,
			.52444,
			0,
			0,
			.89444
		],
		8781: [
			.00222,
			.50222,
			0,
			0,
			.89444
		],
		8801: [
			.00222,
			.50222,
			0,
			0,
			.89444
		],
		8804: [
			.19667,
			.69667,
			0,
			0,
			.89444
		],
		8805: [
			.19667,
			.69667,
			0,
			0,
			.89444
		],
		8810: [
			.08556,
			.58556,
			0,
			0,
			1.14999
		],
		8811: [
			.08556,
			.58556,
			0,
			0,
			1.14999
		],
		8826: [
			.08556,
			.58556,
			0,
			0,
			.89444
		],
		8827: [
			.08556,
			.58556,
			0,
			0,
			.89444
		],
		8834: [
			.08556,
			.58556,
			0,
			0,
			.89444
		],
		8835: [
			.08556,
			.58556,
			0,
			0,
			.89444
		],
		8838: [
			.19667,
			.69667,
			0,
			0,
			.89444
		],
		8839: [
			.19667,
			.69667,
			0,
			0,
			.89444
		],
		8846: [
			0,
			.55556,
			0,
			0,
			.76666
		],
		8849: [
			.19667,
			.69667,
			0,
			0,
			.89444
		],
		8850: [
			.19667,
			.69667,
			0,
			0,
			.89444
		],
		8851: [
			0,
			.55556,
			0,
			0,
			.76666
		],
		8852: [
			0,
			.55556,
			0,
			0,
			.76666
		],
		8853: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		8854: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		8855: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		8856: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		8857: [
			.13333,
			.63333,
			0,
			0,
			.89444
		],
		8866: [
			0,
			.69444,
			0,
			0,
			.70277
		],
		8867: [
			0,
			.69444,
			0,
			0,
			.70277
		],
		8868: [
			0,
			.69444,
			0,
			0,
			.89444
		],
		8869: [
			0,
			.69444,
			0,
			0,
			.89444
		],
		8900: [
			-.02639,
			.47361,
			0,
			0,
			.575
		],
		8901: [
			-.02639,
			.47361,
			0,
			0,
			.31944
		],
		8902: [
			-.02778,
			.47222,
			0,
			0,
			.575
		],
		8968: [
			.25,
			.75,
			0,
			0,
			.51111
		],
		8969: [
			.25,
			.75,
			0,
			0,
			.51111
		],
		8970: [
			.25,
			.75,
			0,
			0,
			.51111
		],
		8971: [
			.25,
			.75,
			0,
			0,
			.51111
		],
		8994: [
			-.13889,
			.36111,
			0,
			0,
			1.14999
		],
		8995: [
			-.13889,
			.36111,
			0,
			0,
			1.14999
		],
		9651: [
			.19444,
			.69444,
			0,
			0,
			1.02222
		],
		9657: [
			-.02778,
			.47222,
			0,
			0,
			.575
		],
		9661: [
			.19444,
			.69444,
			0,
			0,
			1.02222
		],
		9667: [
			-.02778,
			.47222,
			0,
			0,
			.575
		],
		9711: [
			.19444,
			.69444,
			0,
			0,
			1.14999
		],
		9824: [
			.12963,
			.69444,
			0,
			0,
			.89444
		],
		9825: [
			.12963,
			.69444,
			0,
			0,
			.89444
		],
		9826: [
			.12963,
			.69444,
			0,
			0,
			.89444
		],
		9827: [
			.12963,
			.69444,
			0,
			0,
			.89444
		],
		9837: [
			0,
			.75,
			0,
			0,
			.44722
		],
		9838: [
			.19444,
			.69444,
			0,
			0,
			.44722
		],
		9839: [
			.19444,
			.69444,
			0,
			0,
			.44722
		],
		10216: [
			.25,
			.75,
			0,
			0,
			.44722
		],
		10217: [
			.25,
			.75,
			0,
			0,
			.44722
		],
		10815: [
			0,
			.68611,
			0,
			0,
			.9
		],
		10927: [
			.19667,
			.69667,
			0,
			0,
			.89444
		],
		10928: [
			.19667,
			.69667,
			0,
			0,
			.89444
		],
		57376: [
			.19444,
			.69444,
			0,
			0,
			0
		]
	},
	"Main-BoldItalic": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		33: [
			0,
			.69444,
			.11417,
			0,
			.38611
		],
		34: [
			0,
			.69444,
			.07939,
			0,
			.62055
		],
		35: [
			.19444,
			.69444,
			.06833,
			0,
			.94444
		],
		37: [
			.05556,
			.75,
			.12861,
			0,
			.94444
		],
		38: [
			0,
			.69444,
			.08528,
			0,
			.88555
		],
		39: [
			0,
			.69444,
			.12945,
			0,
			.35555
		],
		40: [
			.25,
			.75,
			.15806,
			0,
			.47333
		],
		41: [
			.25,
			.75,
			.03306,
			0,
			.47333
		],
		42: [
			0,
			.75,
			.14333,
			0,
			.59111
		],
		43: [
			.10333,
			.60333,
			.03306,
			0,
			.88555
		],
		44: [
			.19444,
			.14722,
			0,
			0,
			.35555
		],
		45: [
			0,
			.44444,
			.02611,
			0,
			.41444
		],
		46: [
			0,
			.14722,
			0,
			0,
			.35555
		],
		47: [
			.25,
			.75,
			.15806,
			0,
			.59111
		],
		48: [
			0,
			.64444,
			.13167,
			0,
			.59111
		],
		49: [
			0,
			.64444,
			.13167,
			0,
			.59111
		],
		50: [
			0,
			.64444,
			.13167,
			0,
			.59111
		],
		51: [
			0,
			.64444,
			.13167,
			0,
			.59111
		],
		52: [
			.19444,
			.64444,
			.13167,
			0,
			.59111
		],
		53: [
			0,
			.64444,
			.13167,
			0,
			.59111
		],
		54: [
			0,
			.64444,
			.13167,
			0,
			.59111
		],
		55: [
			.19444,
			.64444,
			.13167,
			0,
			.59111
		],
		56: [
			0,
			.64444,
			.13167,
			0,
			.59111
		],
		57: [
			0,
			.64444,
			.13167,
			0,
			.59111
		],
		58: [
			0,
			.44444,
			.06695,
			0,
			.35555
		],
		59: [
			.19444,
			.44444,
			.06695,
			0,
			.35555
		],
		61: [
			-.10889,
			.39111,
			.06833,
			0,
			.88555
		],
		63: [
			0,
			.69444,
			.11472,
			0,
			.59111
		],
		64: [
			0,
			.69444,
			.09208,
			0,
			.88555
		],
		65: [
			0,
			.68611,
			0,
			0,
			.86555
		],
		66: [
			0,
			.68611,
			.0992,
			0,
			.81666
		],
		67: [
			0,
			.68611,
			.14208,
			0,
			.82666
		],
		68: [
			0,
			.68611,
			.09062,
			0,
			.87555
		],
		69: [
			0,
			.68611,
			.11431,
			0,
			.75666
		],
		70: [
			0,
			.68611,
			.12903,
			0,
			.72722
		],
		71: [
			0,
			.68611,
			.07347,
			0,
			.89527
		],
		72: [
			0,
			.68611,
			.17208,
			0,
			.8961
		],
		73: [
			0,
			.68611,
			.15681,
			0,
			.47166
		],
		74: [
			0,
			.68611,
			.145,
			0,
			.61055
		],
		75: [
			0,
			.68611,
			.14208,
			0,
			.89499
		],
		76: [
			0,
			.68611,
			0,
			0,
			.69777
		],
		77: [
			0,
			.68611,
			.17208,
			0,
			1.07277
		],
		78: [
			0,
			.68611,
			.17208,
			0,
			.8961
		],
		79: [
			0,
			.68611,
			.09062,
			0,
			.85499
		],
		80: [
			0,
			.68611,
			.0992,
			0,
			.78721
		],
		81: [
			.19444,
			.68611,
			.09062,
			0,
			.85499
		],
		82: [
			0,
			.68611,
			.02559,
			0,
			.85944
		],
		83: [
			0,
			.68611,
			.11264,
			0,
			.64999
		],
		84: [
			0,
			.68611,
			.12903,
			0,
			.7961
		],
		85: [
			0,
			.68611,
			.17208,
			0,
			.88083
		],
		86: [
			0,
			.68611,
			.18625,
			0,
			.86555
		],
		87: [
			0,
			.68611,
			.18625,
			0,
			1.15999
		],
		88: [
			0,
			.68611,
			.15681,
			0,
			.86555
		],
		89: [
			0,
			.68611,
			.19803,
			0,
			.86555
		],
		90: [
			0,
			.68611,
			.14208,
			0,
			.70888
		],
		91: [
			.25,
			.75,
			.1875,
			0,
			.35611
		],
		93: [
			.25,
			.75,
			.09972,
			0,
			.35611
		],
		94: [
			0,
			.69444,
			.06709,
			0,
			.59111
		],
		95: [
			.31,
			.13444,
			.09811,
			0,
			.59111
		],
		97: [
			0,
			.44444,
			.09426,
			0,
			.59111
		],
		98: [
			0,
			.69444,
			.07861,
			0,
			.53222
		],
		99: [
			0,
			.44444,
			.05222,
			0,
			.53222
		],
		100: [
			0,
			.69444,
			.10861,
			0,
			.59111
		],
		101: [
			0,
			.44444,
			.085,
			0,
			.53222
		],
		102: [
			.19444,
			.69444,
			.21778,
			0,
			.4
		],
		103: [
			.19444,
			.44444,
			.105,
			0,
			.53222
		],
		104: [
			0,
			.69444,
			.09426,
			0,
			.59111
		],
		105: [
			0,
			.69326,
			.11387,
			0,
			.35555
		],
		106: [
			.19444,
			.69326,
			.1672,
			0,
			.35555
		],
		107: [
			0,
			.69444,
			.11111,
			0,
			.53222
		],
		108: [
			0,
			.69444,
			.10861,
			0,
			.29666
		],
		109: [
			0,
			.44444,
			.09426,
			0,
			.94444
		],
		110: [
			0,
			.44444,
			.09426,
			0,
			.64999
		],
		111: [
			0,
			.44444,
			.07861,
			0,
			.59111
		],
		112: [
			.19444,
			.44444,
			.07861,
			0,
			.59111
		],
		113: [
			.19444,
			.44444,
			.105,
			0,
			.53222
		],
		114: [
			0,
			.44444,
			.11111,
			0,
			.50167
		],
		115: [
			0,
			.44444,
			.08167,
			0,
			.48694
		],
		116: [
			0,
			.63492,
			.09639,
			0,
			.385
		],
		117: [
			0,
			.44444,
			.09426,
			0,
			.62055
		],
		118: [
			0,
			.44444,
			.11111,
			0,
			.53222
		],
		119: [
			0,
			.44444,
			.11111,
			0,
			.76777
		],
		120: [
			0,
			.44444,
			.12583,
			0,
			.56055
		],
		121: [
			.19444,
			.44444,
			.105,
			0,
			.56166
		],
		122: [
			0,
			.44444,
			.13889,
			0,
			.49055
		],
		126: [
			.35,
			.34444,
			.11472,
			0,
			.59111
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		168: [
			0,
			.69444,
			.11473,
			0,
			.59111
		],
		176: [
			0,
			.69444,
			0,
			0,
			.94888
		],
		184: [
			.17014,
			0,
			0,
			0,
			.53222
		],
		198: [
			0,
			.68611,
			.11431,
			0,
			1.02277
		],
		216: [
			.04861,
			.73472,
			.09062,
			0,
			.88555
		],
		223: [
			.19444,
			.69444,
			.09736,
			0,
			.665
		],
		230: [
			0,
			.44444,
			.085,
			0,
			.82666
		],
		248: [
			.09722,
			.54167,
			.09458,
			0,
			.59111
		],
		305: [
			0,
			.44444,
			.09426,
			0,
			.35555
		],
		338: [
			0,
			.68611,
			.11431,
			0,
			1.14054
		],
		339: [
			0,
			.44444,
			.085,
			0,
			.82666
		],
		567: [
			.19444,
			.44444,
			.04611,
			0,
			.385
		],
		710: [
			0,
			.69444,
			.06709,
			0,
			.59111
		],
		711: [
			0,
			.63194,
			.08271,
			0,
			.59111
		],
		713: [
			0,
			.59444,
			.10444,
			0,
			.59111
		],
		714: [
			0,
			.69444,
			.08528,
			0,
			.59111
		],
		715: [
			0,
			.69444,
			0,
			0,
			.59111
		],
		728: [
			0,
			.69444,
			.10333,
			0,
			.59111
		],
		729: [
			0,
			.69444,
			.12945,
			0,
			.35555
		],
		730: [
			0,
			.69444,
			0,
			0,
			.94888
		],
		732: [
			0,
			.69444,
			.11472,
			0,
			.59111
		],
		733: [
			0,
			.69444,
			.11472,
			0,
			.59111
		],
		915: [
			0,
			.68611,
			.12903,
			0,
			.69777
		],
		916: [
			0,
			.68611,
			0,
			0,
			.94444
		],
		920: [
			0,
			.68611,
			.09062,
			0,
			.88555
		],
		923: [
			0,
			.68611,
			0,
			0,
			.80666
		],
		926: [
			0,
			.68611,
			.15092,
			0,
			.76777
		],
		928: [
			0,
			.68611,
			.17208,
			0,
			.8961
		],
		931: [
			0,
			.68611,
			.11431,
			0,
			.82666
		],
		933: [
			0,
			.68611,
			.10778,
			0,
			.88555
		],
		934: [
			0,
			.68611,
			.05632,
			0,
			.82666
		],
		936: [
			0,
			.68611,
			.10778,
			0,
			.88555
		],
		937: [
			0,
			.68611,
			.0992,
			0,
			.82666
		],
		8211: [
			0,
			.44444,
			.09811,
			0,
			.59111
		],
		8212: [
			0,
			.44444,
			.09811,
			0,
			1.18221
		],
		8216: [
			0,
			.69444,
			.12945,
			0,
			.35555
		],
		8217: [
			0,
			.69444,
			.12945,
			0,
			.35555
		],
		8220: [
			0,
			.69444,
			.16772,
			0,
			.62055
		],
		8221: [
			0,
			.69444,
			.07939,
			0,
			.62055
		]
	},
	"Main-Italic": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		33: [
			0,
			.69444,
			.12417,
			0,
			.30667
		],
		34: [
			0,
			.69444,
			.06961,
			0,
			.51444
		],
		35: [
			.19444,
			.69444,
			.06616,
			0,
			.81777
		],
		37: [
			.05556,
			.75,
			.13639,
			0,
			.81777
		],
		38: [
			0,
			.69444,
			.09694,
			0,
			.76666
		],
		39: [
			0,
			.69444,
			.12417,
			0,
			.30667
		],
		40: [
			.25,
			.75,
			.16194,
			0,
			.40889
		],
		41: [
			.25,
			.75,
			.03694,
			0,
			.40889
		],
		42: [
			0,
			.75,
			.14917,
			0,
			.51111
		],
		43: [
			.05667,
			.56167,
			.03694,
			0,
			.76666
		],
		44: [
			.19444,
			.10556,
			0,
			0,
			.30667
		],
		45: [
			0,
			.43056,
			.02826,
			0,
			.35778
		],
		46: [
			0,
			.10556,
			0,
			0,
			.30667
		],
		47: [
			.25,
			.75,
			.16194,
			0,
			.51111
		],
		48: [
			0,
			.64444,
			.13556,
			0,
			.51111
		],
		49: [
			0,
			.64444,
			.13556,
			0,
			.51111
		],
		50: [
			0,
			.64444,
			.13556,
			0,
			.51111
		],
		51: [
			0,
			.64444,
			.13556,
			0,
			.51111
		],
		52: [
			.19444,
			.64444,
			.13556,
			0,
			.51111
		],
		53: [
			0,
			.64444,
			.13556,
			0,
			.51111
		],
		54: [
			0,
			.64444,
			.13556,
			0,
			.51111
		],
		55: [
			.19444,
			.64444,
			.13556,
			0,
			.51111
		],
		56: [
			0,
			.64444,
			.13556,
			0,
			.51111
		],
		57: [
			0,
			.64444,
			.13556,
			0,
			.51111
		],
		58: [
			0,
			.43056,
			.0582,
			0,
			.30667
		],
		59: [
			.19444,
			.43056,
			.0582,
			0,
			.30667
		],
		61: [
			-.13313,
			.36687,
			.06616,
			0,
			.76666
		],
		63: [
			0,
			.69444,
			.1225,
			0,
			.51111
		],
		64: [
			0,
			.69444,
			.09597,
			0,
			.76666
		],
		65: [
			0,
			.68333,
			0,
			0,
			.74333
		],
		66: [
			0,
			.68333,
			.10257,
			0,
			.70389
		],
		67: [
			0,
			.68333,
			.14528,
			0,
			.71555
		],
		68: [
			0,
			.68333,
			.09403,
			0,
			.755
		],
		69: [
			0,
			.68333,
			.12028,
			0,
			.67833
		],
		70: [
			0,
			.68333,
			.13305,
			0,
			.65277
		],
		71: [
			0,
			.68333,
			.08722,
			0,
			.77361
		],
		72: [
			0,
			.68333,
			.16389,
			0,
			.74333
		],
		73: [
			0,
			.68333,
			.15806,
			0,
			.38555
		],
		74: [
			0,
			.68333,
			.14028,
			0,
			.525
		],
		75: [
			0,
			.68333,
			.14528,
			0,
			.76888
		],
		76: [
			0,
			.68333,
			0,
			0,
			.62722
		],
		77: [
			0,
			.68333,
			.16389,
			0,
			.89666
		],
		78: [
			0,
			.68333,
			.16389,
			0,
			.74333
		],
		79: [
			0,
			.68333,
			.09403,
			0,
			.76666
		],
		80: [
			0,
			.68333,
			.10257,
			0,
			.67833
		],
		81: [
			.19444,
			.68333,
			.09403,
			0,
			.76666
		],
		82: [
			0,
			.68333,
			.03868,
			0,
			.72944
		],
		83: [
			0,
			.68333,
			.11972,
			0,
			.56222
		],
		84: [
			0,
			.68333,
			.13305,
			0,
			.71555
		],
		85: [
			0,
			.68333,
			.16389,
			0,
			.74333
		],
		86: [
			0,
			.68333,
			.18361,
			0,
			.74333
		],
		87: [
			0,
			.68333,
			.18361,
			0,
			.99888
		],
		88: [
			0,
			.68333,
			.15806,
			0,
			.74333
		],
		89: [
			0,
			.68333,
			.19383,
			0,
			.74333
		],
		90: [
			0,
			.68333,
			.14528,
			0,
			.61333
		],
		91: [
			.25,
			.75,
			.1875,
			0,
			.30667
		],
		93: [
			.25,
			.75,
			.10528,
			0,
			.30667
		],
		94: [
			0,
			.69444,
			.06646,
			0,
			.51111
		],
		95: [
			.31,
			.12056,
			.09208,
			0,
			.51111
		],
		97: [
			0,
			.43056,
			.07671,
			0,
			.51111
		],
		98: [
			0,
			.69444,
			.06312,
			0,
			.46
		],
		99: [
			0,
			.43056,
			.05653,
			0,
			.46
		],
		100: [
			0,
			.69444,
			.10333,
			0,
			.51111
		],
		101: [
			0,
			.43056,
			.07514,
			0,
			.46
		],
		102: [
			.19444,
			.69444,
			.21194,
			0,
			.30667
		],
		103: [
			.19444,
			.43056,
			.08847,
			0,
			.46
		],
		104: [
			0,
			.69444,
			.07671,
			0,
			.51111
		],
		105: [
			0,
			.65536,
			.1019,
			0,
			.30667
		],
		106: [
			.19444,
			.65536,
			.14467,
			0,
			.30667
		],
		107: [
			0,
			.69444,
			.10764,
			0,
			.46
		],
		108: [
			0,
			.69444,
			.10333,
			0,
			.25555
		],
		109: [
			0,
			.43056,
			.07671,
			0,
			.81777
		],
		110: [
			0,
			.43056,
			.07671,
			0,
			.56222
		],
		111: [
			0,
			.43056,
			.06312,
			0,
			.51111
		],
		112: [
			.19444,
			.43056,
			.06312,
			0,
			.51111
		],
		113: [
			.19444,
			.43056,
			.08847,
			0,
			.46
		],
		114: [
			0,
			.43056,
			.10764,
			0,
			.42166
		],
		115: [
			0,
			.43056,
			.08208,
			0,
			.40889
		],
		116: [
			0,
			.61508,
			.09486,
			0,
			.33222
		],
		117: [
			0,
			.43056,
			.07671,
			0,
			.53666
		],
		118: [
			0,
			.43056,
			.10764,
			0,
			.46
		],
		119: [
			0,
			.43056,
			.10764,
			0,
			.66444
		],
		120: [
			0,
			.43056,
			.12042,
			0,
			.46389
		],
		121: [
			.19444,
			.43056,
			.08847,
			0,
			.48555
		],
		122: [
			0,
			.43056,
			.12292,
			0,
			.40889
		],
		126: [
			.35,
			.31786,
			.11585,
			0,
			.51111
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		168: [
			0,
			.66786,
			.10474,
			0,
			.51111
		],
		176: [
			0,
			.69444,
			0,
			0,
			.83129
		],
		184: [
			.17014,
			0,
			0,
			0,
			.46
		],
		198: [
			0,
			.68333,
			.12028,
			0,
			.88277
		],
		216: [
			.04861,
			.73194,
			.09403,
			0,
			.76666
		],
		223: [
			.19444,
			.69444,
			.10514,
			0,
			.53666
		],
		230: [
			0,
			.43056,
			.07514,
			0,
			.71555
		],
		248: [
			.09722,
			.52778,
			.09194,
			0,
			.51111
		],
		338: [
			0,
			.68333,
			.12028,
			0,
			.98499
		],
		339: [
			0,
			.43056,
			.07514,
			0,
			.71555
		],
		710: [
			0,
			.69444,
			.06646,
			0,
			.51111
		],
		711: [
			0,
			.62847,
			.08295,
			0,
			.51111
		],
		713: [
			0,
			.56167,
			.10333,
			0,
			.51111
		],
		714: [
			0,
			.69444,
			.09694,
			0,
			.51111
		],
		715: [
			0,
			.69444,
			0,
			0,
			.51111
		],
		728: [
			0,
			.69444,
			.10806,
			0,
			.51111
		],
		729: [
			0,
			.66786,
			.11752,
			0,
			.30667
		],
		730: [
			0,
			.69444,
			0,
			0,
			.83129
		],
		732: [
			0,
			.66786,
			.11585,
			0,
			.51111
		],
		733: [
			0,
			.69444,
			.1225,
			0,
			.51111
		],
		915: [
			0,
			.68333,
			.13305,
			0,
			.62722
		],
		916: [
			0,
			.68333,
			0,
			0,
			.81777
		],
		920: [
			0,
			.68333,
			.09403,
			0,
			.76666
		],
		923: [
			0,
			.68333,
			0,
			0,
			.69222
		],
		926: [
			0,
			.68333,
			.15294,
			0,
			.66444
		],
		928: [
			0,
			.68333,
			.16389,
			0,
			.74333
		],
		931: [
			0,
			.68333,
			.12028,
			0,
			.71555
		],
		933: [
			0,
			.68333,
			.11111,
			0,
			.76666
		],
		934: [
			0,
			.68333,
			.05986,
			0,
			.71555
		],
		936: [
			0,
			.68333,
			.11111,
			0,
			.76666
		],
		937: [
			0,
			.68333,
			.10257,
			0,
			.71555
		],
		8211: [
			0,
			.43056,
			.09208,
			0,
			.51111
		],
		8212: [
			0,
			.43056,
			.09208,
			0,
			1.02222
		],
		8216: [
			0,
			.69444,
			.12417,
			0,
			.30667
		],
		8217: [
			0,
			.69444,
			.12417,
			0,
			.30667
		],
		8220: [
			0,
			.69444,
			.1685,
			0,
			.51444
		],
		8221: [
			0,
			.69444,
			.06961,
			0,
			.51444
		],
		8463: [
			0,
			.68889,
			0,
			0,
			.54028
		]
	},
	"Main-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		33: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		34: [
			0,
			.69444,
			0,
			0,
			.5
		],
		35: [
			.19444,
			.69444,
			0,
			0,
			.83334
		],
		36: [
			.05556,
			.75,
			0,
			0,
			.5
		],
		37: [
			.05556,
			.75,
			0,
			0,
			.83334
		],
		38: [
			0,
			.69444,
			0,
			0,
			.77778
		],
		39: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		40: [
			.25,
			.75,
			0,
			0,
			.38889
		],
		41: [
			.25,
			.75,
			0,
			0,
			.38889
		],
		42: [
			0,
			.75,
			0,
			0,
			.5
		],
		43: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		44: [
			.19444,
			.10556,
			0,
			0,
			.27778
		],
		45: [
			0,
			.43056,
			0,
			0,
			.33333
		],
		46: [
			0,
			.10556,
			0,
			0,
			.27778
		],
		47: [
			.25,
			.75,
			0,
			0,
			.5
		],
		48: [
			0,
			.64444,
			0,
			0,
			.5
		],
		49: [
			0,
			.64444,
			0,
			0,
			.5
		],
		50: [
			0,
			.64444,
			0,
			0,
			.5
		],
		51: [
			0,
			.64444,
			0,
			0,
			.5
		],
		52: [
			0,
			.64444,
			0,
			0,
			.5
		],
		53: [
			0,
			.64444,
			0,
			0,
			.5
		],
		54: [
			0,
			.64444,
			0,
			0,
			.5
		],
		55: [
			0,
			.64444,
			0,
			0,
			.5
		],
		56: [
			0,
			.64444,
			0,
			0,
			.5
		],
		57: [
			0,
			.64444,
			0,
			0,
			.5
		],
		58: [
			0,
			.43056,
			0,
			0,
			.27778
		],
		59: [
			.19444,
			.43056,
			0,
			0,
			.27778
		],
		60: [
			.0391,
			.5391,
			0,
			0,
			.77778
		],
		61: [
			-.13313,
			.36687,
			0,
			0,
			.77778
		],
		62: [
			.0391,
			.5391,
			0,
			0,
			.77778
		],
		63: [
			0,
			.69444,
			0,
			0,
			.47222
		],
		64: [
			0,
			.69444,
			0,
			0,
			.77778
		],
		65: [
			0,
			.68333,
			0,
			0,
			.75
		],
		66: [
			0,
			.68333,
			0,
			0,
			.70834
		],
		67: [
			0,
			.68333,
			0,
			0,
			.72222
		],
		68: [
			0,
			.68333,
			0,
			0,
			.76389
		],
		69: [
			0,
			.68333,
			0,
			0,
			.68056
		],
		70: [
			0,
			.68333,
			0,
			0,
			.65278
		],
		71: [
			0,
			.68333,
			0,
			0,
			.78472
		],
		72: [
			0,
			.68333,
			0,
			0,
			.75
		],
		73: [
			0,
			.68333,
			0,
			0,
			.36111
		],
		74: [
			0,
			.68333,
			0,
			0,
			.51389
		],
		75: [
			0,
			.68333,
			0,
			0,
			.77778
		],
		76: [
			0,
			.68333,
			0,
			0,
			.625
		],
		77: [
			0,
			.68333,
			0,
			0,
			.91667
		],
		78: [
			0,
			.68333,
			0,
			0,
			.75
		],
		79: [
			0,
			.68333,
			0,
			0,
			.77778
		],
		80: [
			0,
			.68333,
			0,
			0,
			.68056
		],
		81: [
			.19444,
			.68333,
			0,
			0,
			.77778
		],
		82: [
			0,
			.68333,
			0,
			0,
			.73611
		],
		83: [
			0,
			.68333,
			0,
			0,
			.55556
		],
		84: [
			0,
			.68333,
			0,
			0,
			.72222
		],
		85: [
			0,
			.68333,
			0,
			0,
			.75
		],
		86: [
			0,
			.68333,
			.01389,
			0,
			.75
		],
		87: [
			0,
			.68333,
			.01389,
			0,
			1.02778
		],
		88: [
			0,
			.68333,
			0,
			0,
			.75
		],
		89: [
			0,
			.68333,
			.025,
			0,
			.75
		],
		90: [
			0,
			.68333,
			0,
			0,
			.61111
		],
		91: [
			.25,
			.75,
			0,
			0,
			.27778
		],
		92: [
			.25,
			.75,
			0,
			0,
			.5
		],
		93: [
			.25,
			.75,
			0,
			0,
			.27778
		],
		94: [
			0,
			.69444,
			0,
			0,
			.5
		],
		95: [
			.31,
			.12056,
			.02778,
			0,
			.5
		],
		97: [
			0,
			.43056,
			0,
			0,
			.5
		],
		98: [
			0,
			.69444,
			0,
			0,
			.55556
		],
		99: [
			0,
			.43056,
			0,
			0,
			.44445
		],
		100: [
			0,
			.69444,
			0,
			0,
			.55556
		],
		101: [
			0,
			.43056,
			0,
			0,
			.44445
		],
		102: [
			0,
			.69444,
			.07778,
			0,
			.30556
		],
		103: [
			.19444,
			.43056,
			.01389,
			0,
			.5
		],
		104: [
			0,
			.69444,
			0,
			0,
			.55556
		],
		105: [
			0,
			.66786,
			0,
			0,
			.27778
		],
		106: [
			.19444,
			.66786,
			0,
			0,
			.30556
		],
		107: [
			0,
			.69444,
			0,
			0,
			.52778
		],
		108: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		109: [
			0,
			.43056,
			0,
			0,
			.83334
		],
		110: [
			0,
			.43056,
			0,
			0,
			.55556
		],
		111: [
			0,
			.43056,
			0,
			0,
			.5
		],
		112: [
			.19444,
			.43056,
			0,
			0,
			.55556
		],
		113: [
			.19444,
			.43056,
			0,
			0,
			.52778
		],
		114: [
			0,
			.43056,
			0,
			0,
			.39167
		],
		115: [
			0,
			.43056,
			0,
			0,
			.39445
		],
		116: [
			0,
			.61508,
			0,
			0,
			.38889
		],
		117: [
			0,
			.43056,
			0,
			0,
			.55556
		],
		118: [
			0,
			.43056,
			.01389,
			0,
			.52778
		],
		119: [
			0,
			.43056,
			.01389,
			0,
			.72222
		],
		120: [
			0,
			.43056,
			0,
			0,
			.52778
		],
		121: [
			.19444,
			.43056,
			.01389,
			0,
			.52778
		],
		122: [
			0,
			.43056,
			0,
			0,
			.44445
		],
		123: [
			.25,
			.75,
			0,
			0,
			.5
		],
		124: [
			.25,
			.75,
			0,
			0,
			.27778
		],
		125: [
			.25,
			.75,
			0,
			0,
			.5
		],
		126: [
			.35,
			.31786,
			0,
			0,
			.5
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		163: [
			0,
			.69444,
			0,
			0,
			.76909
		],
		167: [
			.19444,
			.69444,
			0,
			0,
			.44445
		],
		168: [
			0,
			.66786,
			0,
			0,
			.5
		],
		172: [
			0,
			.43056,
			0,
			0,
			.66667
		],
		176: [
			0,
			.69444,
			0,
			0,
			.75
		],
		177: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		182: [
			.19444,
			.69444,
			0,
			0,
			.61111
		],
		184: [
			.17014,
			0,
			0,
			0,
			.44445
		],
		198: [
			0,
			.68333,
			0,
			0,
			.90278
		],
		215: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		216: [
			.04861,
			.73194,
			0,
			0,
			.77778
		],
		223: [
			0,
			.69444,
			0,
			0,
			.5
		],
		230: [
			0,
			.43056,
			0,
			0,
			.72222
		],
		247: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		248: [
			.09722,
			.52778,
			0,
			0,
			.5
		],
		305: [
			0,
			.43056,
			0,
			0,
			.27778
		],
		338: [
			0,
			.68333,
			0,
			0,
			1.01389
		],
		339: [
			0,
			.43056,
			0,
			0,
			.77778
		],
		567: [
			.19444,
			.43056,
			0,
			0,
			.30556
		],
		710: [
			0,
			.69444,
			0,
			0,
			.5
		],
		711: [
			0,
			.62847,
			0,
			0,
			.5
		],
		713: [
			0,
			.56778,
			0,
			0,
			.5
		],
		714: [
			0,
			.69444,
			0,
			0,
			.5
		],
		715: [
			0,
			.69444,
			0,
			0,
			.5
		],
		728: [
			0,
			.69444,
			0,
			0,
			.5
		],
		729: [
			0,
			.66786,
			0,
			0,
			.27778
		],
		730: [
			0,
			.69444,
			0,
			0,
			.75
		],
		732: [
			0,
			.66786,
			0,
			0,
			.5
		],
		733: [
			0,
			.69444,
			0,
			0,
			.5
		],
		915: [
			0,
			.68333,
			0,
			0,
			.625
		],
		916: [
			0,
			.68333,
			0,
			0,
			.83334
		],
		920: [
			0,
			.68333,
			0,
			0,
			.77778
		],
		923: [
			0,
			.68333,
			0,
			0,
			.69445
		],
		926: [
			0,
			.68333,
			0,
			0,
			.66667
		],
		928: [
			0,
			.68333,
			0,
			0,
			.75
		],
		931: [
			0,
			.68333,
			0,
			0,
			.72222
		],
		933: [
			0,
			.68333,
			0,
			0,
			.77778
		],
		934: [
			0,
			.68333,
			0,
			0,
			.72222
		],
		936: [
			0,
			.68333,
			0,
			0,
			.77778
		],
		937: [
			0,
			.68333,
			0,
			0,
			.72222
		],
		8211: [
			0,
			.43056,
			.02778,
			0,
			.5
		],
		8212: [
			0,
			.43056,
			.02778,
			0,
			1
		],
		8216: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		8217: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		8220: [
			0,
			.69444,
			0,
			0,
			.5
		],
		8221: [
			0,
			.69444,
			0,
			0,
			.5
		],
		8224: [
			.19444,
			.69444,
			0,
			0,
			.44445
		],
		8225: [
			.19444,
			.69444,
			0,
			0,
			.44445
		],
		8230: [
			0,
			.123,
			0,
			0,
			1.172
		],
		8242: [
			0,
			.55556,
			0,
			0,
			.275
		],
		8407: [
			0,
			.71444,
			.15382,
			0,
			.5
		],
		8463: [
			0,
			.68889,
			0,
			0,
			.54028
		],
		8465: [
			0,
			.69444,
			0,
			0,
			.72222
		],
		8467: [
			0,
			.69444,
			0,
			.11111,
			.41667
		],
		8472: [
			.19444,
			.43056,
			0,
			.11111,
			.63646
		],
		8476: [
			0,
			.69444,
			0,
			0,
			.72222
		],
		8501: [
			0,
			.69444,
			0,
			0,
			.61111
		],
		8592: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8593: [
			.19444,
			.69444,
			0,
			0,
			.5
		],
		8594: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8595: [
			.19444,
			.69444,
			0,
			0,
			.5
		],
		8596: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8597: [
			.25,
			.75,
			0,
			0,
			.5
		],
		8598: [
			.19444,
			.69444,
			0,
			0,
			1
		],
		8599: [
			.19444,
			.69444,
			0,
			0,
			1
		],
		8600: [
			.19444,
			.69444,
			0,
			0,
			1
		],
		8601: [
			.19444,
			.69444,
			0,
			0,
			1
		],
		8614: [
			.011,
			.511,
			0,
			0,
			1
		],
		8617: [
			.011,
			.511,
			0,
			0,
			1.126
		],
		8618: [
			.011,
			.511,
			0,
			0,
			1.126
		],
		8636: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8637: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8640: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8641: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8652: [
			.011,
			.671,
			0,
			0,
			1
		],
		8656: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8657: [
			.19444,
			.69444,
			0,
			0,
			.61111
		],
		8658: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8659: [
			.19444,
			.69444,
			0,
			0,
			.61111
		],
		8660: [
			-.13313,
			.36687,
			0,
			0,
			1
		],
		8661: [
			.25,
			.75,
			0,
			0,
			.61111
		],
		8704: [
			0,
			.69444,
			0,
			0,
			.55556
		],
		8706: [
			0,
			.69444,
			.05556,
			.08334,
			.5309
		],
		8707: [
			0,
			.69444,
			0,
			0,
			.55556
		],
		8709: [
			.05556,
			.75,
			0,
			0,
			.5
		],
		8711: [
			0,
			.68333,
			0,
			0,
			.83334
		],
		8712: [
			.0391,
			.5391,
			0,
			0,
			.66667
		],
		8715: [
			.0391,
			.5391,
			0,
			0,
			.66667
		],
		8722: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		8723: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		8725: [
			.25,
			.75,
			0,
			0,
			.5
		],
		8726: [
			.25,
			.75,
			0,
			0,
			.5
		],
		8727: [
			-.03472,
			.46528,
			0,
			0,
			.5
		],
		8728: [
			-.05555,
			.44445,
			0,
			0,
			.5
		],
		8729: [
			-.05555,
			.44445,
			0,
			0,
			.5
		],
		8730: [
			.2,
			.8,
			0,
			0,
			.83334
		],
		8733: [
			0,
			.43056,
			0,
			0,
			.77778
		],
		8734: [
			0,
			.43056,
			0,
			0,
			1
		],
		8736: [
			0,
			.69224,
			0,
			0,
			.72222
		],
		8739: [
			.25,
			.75,
			0,
			0,
			.27778
		],
		8741: [
			.25,
			.75,
			0,
			0,
			.5
		],
		8743: [
			0,
			.55556,
			0,
			0,
			.66667
		],
		8744: [
			0,
			.55556,
			0,
			0,
			.66667
		],
		8745: [
			0,
			.55556,
			0,
			0,
			.66667
		],
		8746: [
			0,
			.55556,
			0,
			0,
			.66667
		],
		8747: [
			.19444,
			.69444,
			.11111,
			0,
			.41667
		],
		8764: [
			-.13313,
			.36687,
			0,
			0,
			.77778
		],
		8768: [
			.19444,
			.69444,
			0,
			0,
			.27778
		],
		8771: [
			-.03625,
			.46375,
			0,
			0,
			.77778
		],
		8773: [
			-.022,
			.589,
			0,
			0,
			.778
		],
		8776: [
			-.01688,
			.48312,
			0,
			0,
			.77778
		],
		8781: [
			-.03625,
			.46375,
			0,
			0,
			.77778
		],
		8784: [
			-.133,
			.673,
			0,
			0,
			.778
		],
		8801: [
			-.03625,
			.46375,
			0,
			0,
			.77778
		],
		8804: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		8805: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		8810: [
			.0391,
			.5391,
			0,
			0,
			1
		],
		8811: [
			.0391,
			.5391,
			0,
			0,
			1
		],
		8826: [
			.0391,
			.5391,
			0,
			0,
			.77778
		],
		8827: [
			.0391,
			.5391,
			0,
			0,
			.77778
		],
		8834: [
			.0391,
			.5391,
			0,
			0,
			.77778
		],
		8835: [
			.0391,
			.5391,
			0,
			0,
			.77778
		],
		8838: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		8839: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		8846: [
			0,
			.55556,
			0,
			0,
			.66667
		],
		8849: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		8850: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		8851: [
			0,
			.55556,
			0,
			0,
			.66667
		],
		8852: [
			0,
			.55556,
			0,
			0,
			.66667
		],
		8853: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		8854: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		8855: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		8856: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		8857: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		8866: [
			0,
			.69444,
			0,
			0,
			.61111
		],
		8867: [
			0,
			.69444,
			0,
			0,
			.61111
		],
		8868: [
			0,
			.69444,
			0,
			0,
			.77778
		],
		8869: [
			0,
			.69444,
			0,
			0,
			.77778
		],
		8872: [
			.249,
			.75,
			0,
			0,
			.867
		],
		8900: [
			-.05555,
			.44445,
			0,
			0,
			.5
		],
		8901: [
			-.05555,
			.44445,
			0,
			0,
			.27778
		],
		8902: [
			-.03472,
			.46528,
			0,
			0,
			.5
		],
		8904: [
			.005,
			.505,
			0,
			0,
			.9
		],
		8942: [
			.03,
			.903,
			0,
			0,
			.278
		],
		8943: [
			-.19,
			.313,
			0,
			0,
			1.172
		],
		8945: [
			-.1,
			.823,
			0,
			0,
			1.282
		],
		8968: [
			.25,
			.75,
			0,
			0,
			.44445
		],
		8969: [
			.25,
			.75,
			0,
			0,
			.44445
		],
		8970: [
			.25,
			.75,
			0,
			0,
			.44445
		],
		8971: [
			.25,
			.75,
			0,
			0,
			.44445
		],
		8994: [
			-.14236,
			.35764,
			0,
			0,
			1
		],
		8995: [
			-.14236,
			.35764,
			0,
			0,
			1
		],
		9136: [
			.244,
			.744,
			0,
			0,
			.412
		],
		9137: [
			.244,
			.745,
			0,
			0,
			.412
		],
		9651: [
			.19444,
			.69444,
			0,
			0,
			.88889
		],
		9657: [
			-.03472,
			.46528,
			0,
			0,
			.5
		],
		9661: [
			.19444,
			.69444,
			0,
			0,
			.88889
		],
		9667: [
			-.03472,
			.46528,
			0,
			0,
			.5
		],
		9711: [
			.19444,
			.69444,
			0,
			0,
			1
		],
		9824: [
			.12963,
			.69444,
			0,
			0,
			.77778
		],
		9825: [
			.12963,
			.69444,
			0,
			0,
			.77778
		],
		9826: [
			.12963,
			.69444,
			0,
			0,
			.77778
		],
		9827: [
			.12963,
			.69444,
			0,
			0,
			.77778
		],
		9837: [
			0,
			.75,
			0,
			0,
			.38889
		],
		9838: [
			.19444,
			.69444,
			0,
			0,
			.38889
		],
		9839: [
			.19444,
			.69444,
			0,
			0,
			.38889
		],
		10216: [
			.25,
			.75,
			0,
			0,
			.38889
		],
		10217: [
			.25,
			.75,
			0,
			0,
			.38889
		],
		10222: [
			.244,
			.744,
			0,
			0,
			.412
		],
		10223: [
			.244,
			.745,
			0,
			0,
			.412
		],
		10229: [
			.011,
			.511,
			0,
			0,
			1.609
		],
		10230: [
			.011,
			.511,
			0,
			0,
			1.638
		],
		10231: [
			.011,
			.511,
			0,
			0,
			1.859
		],
		10232: [
			.024,
			.525,
			0,
			0,
			1.609
		],
		10233: [
			.024,
			.525,
			0,
			0,
			1.638
		],
		10234: [
			.024,
			.525,
			0,
			0,
			1.858
		],
		10236: [
			.011,
			.511,
			0,
			0,
			1.638
		],
		10815: [
			0,
			.68333,
			0,
			0,
			.75
		],
		10927: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		10928: [
			.13597,
			.63597,
			0,
			0,
			.77778
		],
		57376: [
			.19444,
			.69444,
			0,
			0,
			0
		]
	},
	"Math-BoldItalic": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		48: [
			0,
			.44444,
			0,
			0,
			.575
		],
		49: [
			0,
			.44444,
			0,
			0,
			.575
		],
		50: [
			0,
			.44444,
			0,
			0,
			.575
		],
		51: [
			.19444,
			.44444,
			0,
			0,
			.575
		],
		52: [
			.19444,
			.44444,
			0,
			0,
			.575
		],
		53: [
			.19444,
			.44444,
			0,
			0,
			.575
		],
		54: [
			0,
			.64444,
			0,
			0,
			.575
		],
		55: [
			.19444,
			.44444,
			0,
			0,
			.575
		],
		56: [
			0,
			.64444,
			0,
			0,
			.575
		],
		57: [
			.19444,
			.44444,
			0,
			0,
			.575
		],
		65: [
			0,
			.68611,
			0,
			0,
			.86944
		],
		66: [
			0,
			.68611,
			.04835,
			0,
			.8664
		],
		67: [
			0,
			.68611,
			.06979,
			0,
			.81694
		],
		68: [
			0,
			.68611,
			.03194,
			0,
			.93812
		],
		69: [
			0,
			.68611,
			.05451,
			0,
			.81007
		],
		70: [
			0,
			.68611,
			.15972,
			0,
			.68889
		],
		71: [
			0,
			.68611,
			0,
			0,
			.88673
		],
		72: [
			0,
			.68611,
			.08229,
			0,
			.98229
		],
		73: [
			0,
			.68611,
			.07778,
			0,
			.51111
		],
		74: [
			0,
			.68611,
			.10069,
			0,
			.63125
		],
		75: [
			0,
			.68611,
			.06979,
			0,
			.97118
		],
		76: [
			0,
			.68611,
			0,
			0,
			.75555
		],
		77: [
			0,
			.68611,
			.11424,
			0,
			1.14201
		],
		78: [
			0,
			.68611,
			.11424,
			0,
			.95034
		],
		79: [
			0,
			.68611,
			.03194,
			0,
			.83666
		],
		80: [
			0,
			.68611,
			.15972,
			0,
			.72309
		],
		81: [
			.19444,
			.68611,
			0,
			0,
			.86861
		],
		82: [
			0,
			.68611,
			.00421,
			0,
			.87235
		],
		83: [
			0,
			.68611,
			.05382,
			0,
			.69271
		],
		84: [
			0,
			.68611,
			.15972,
			0,
			.63663
		],
		85: [
			0,
			.68611,
			.11424,
			0,
			.80027
		],
		86: [
			0,
			.68611,
			.25555,
			0,
			.67778
		],
		87: [
			0,
			.68611,
			.15972,
			0,
			1.09305
		],
		88: [
			0,
			.68611,
			.07778,
			0,
			.94722
		],
		89: [
			0,
			.68611,
			.25555,
			0,
			.67458
		],
		90: [
			0,
			.68611,
			.06979,
			0,
			.77257
		],
		97: [
			0,
			.44444,
			0,
			0,
			.63287
		],
		98: [
			0,
			.69444,
			0,
			0,
			.52083
		],
		99: [
			0,
			.44444,
			0,
			0,
			.51342
		],
		100: [
			0,
			.69444,
			0,
			0,
			.60972
		],
		101: [
			0,
			.44444,
			0,
			0,
			.55361
		],
		102: [
			.19444,
			.69444,
			.11042,
			0,
			.56806
		],
		103: [
			.19444,
			.44444,
			.03704,
			0,
			.5449
		],
		104: [
			0,
			.69444,
			0,
			0,
			.66759
		],
		105: [
			0,
			.69326,
			0,
			0,
			.4048
		],
		106: [
			.19444,
			.69326,
			.0622,
			0,
			.47083
		],
		107: [
			0,
			.69444,
			.01852,
			0,
			.6037
		],
		108: [
			0,
			.69444,
			.0088,
			0,
			.34815
		],
		109: [
			0,
			.44444,
			0,
			0,
			1.0324
		],
		110: [
			0,
			.44444,
			0,
			0,
			.71296
		],
		111: [
			0,
			.44444,
			0,
			0,
			.58472
		],
		112: [
			.19444,
			.44444,
			0,
			0,
			.60092
		],
		113: [
			.19444,
			.44444,
			.03704,
			0,
			.54213
		],
		114: [
			0,
			.44444,
			.03194,
			0,
			.5287
		],
		115: [
			0,
			.44444,
			0,
			0,
			.53125
		],
		116: [
			0,
			.63492,
			0,
			0,
			.41528
		],
		117: [
			0,
			.44444,
			0,
			0,
			.68102
		],
		118: [
			0,
			.44444,
			.03704,
			0,
			.56666
		],
		119: [
			0,
			.44444,
			.02778,
			0,
			.83148
		],
		120: [
			0,
			.44444,
			0,
			0,
			.65903
		],
		121: [
			.19444,
			.44444,
			.03704,
			0,
			.59028
		],
		122: [
			0,
			.44444,
			.04213,
			0,
			.55509
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		915: [
			0,
			.68611,
			.15972,
			0,
			.65694
		],
		916: [
			0,
			.68611,
			0,
			0,
			.95833
		],
		920: [
			0,
			.68611,
			.03194,
			0,
			.86722
		],
		923: [
			0,
			.68611,
			0,
			0,
			.80555
		],
		926: [
			0,
			.68611,
			.07458,
			0,
			.84125
		],
		928: [
			0,
			.68611,
			.08229,
			0,
			.98229
		],
		931: [
			0,
			.68611,
			.05451,
			0,
			.88507
		],
		933: [
			0,
			.68611,
			.15972,
			0,
			.67083
		],
		934: [
			0,
			.68611,
			0,
			0,
			.76666
		],
		936: [
			0,
			.68611,
			.11653,
			0,
			.71402
		],
		937: [
			0,
			.68611,
			.04835,
			0,
			.8789
		],
		945: [
			0,
			.44444,
			0,
			0,
			.76064
		],
		946: [
			.19444,
			.69444,
			.03403,
			0,
			.65972
		],
		947: [
			.19444,
			.44444,
			.06389,
			0,
			.59003
		],
		948: [
			0,
			.69444,
			.03819,
			0,
			.52222
		],
		949: [
			0,
			.44444,
			0,
			0,
			.52882
		],
		950: [
			.19444,
			.69444,
			.06215,
			0,
			.50833
		],
		951: [
			.19444,
			.44444,
			.03704,
			0,
			.6
		],
		952: [
			0,
			.69444,
			.03194,
			0,
			.5618
		],
		953: [
			0,
			.44444,
			0,
			0,
			.41204
		],
		954: [
			0,
			.44444,
			0,
			0,
			.66759
		],
		955: [
			0,
			.69444,
			0,
			0,
			.67083
		],
		956: [
			.19444,
			.44444,
			0,
			0,
			.70787
		],
		957: [
			0,
			.44444,
			.06898,
			0,
			.57685
		],
		958: [
			.19444,
			.69444,
			.03021,
			0,
			.50833
		],
		959: [
			0,
			.44444,
			0,
			0,
			.58472
		],
		960: [
			0,
			.44444,
			.03704,
			0,
			.68241
		],
		961: [
			.19444,
			.44444,
			0,
			0,
			.6118
		],
		962: [
			.09722,
			.44444,
			.07917,
			0,
			.42361
		],
		963: [
			0,
			.44444,
			.03704,
			0,
			.68588
		],
		964: [
			0,
			.44444,
			.13472,
			0,
			.52083
		],
		965: [
			0,
			.44444,
			.03704,
			0,
			.63055
		],
		966: [
			.19444,
			.44444,
			0,
			0,
			.74722
		],
		967: [
			.19444,
			.44444,
			0,
			0,
			.71805
		],
		968: [
			.19444,
			.69444,
			.03704,
			0,
			.75833
		],
		969: [
			0,
			.44444,
			.03704,
			0,
			.71782
		],
		977: [
			0,
			.69444,
			0,
			0,
			.69155
		],
		981: [
			.19444,
			.69444,
			0,
			0,
			.7125
		],
		982: [
			0,
			.44444,
			.03194,
			0,
			.975
		],
		1009: [
			.19444,
			.44444,
			0,
			0,
			.6118
		],
		1013: [
			0,
			.44444,
			0,
			0,
			.48333
		],
		57649: [
			0,
			.44444,
			0,
			0,
			.39352
		],
		57911: [
			.19444,
			.44444,
			0,
			0,
			.43889
		]
	},
	"Math-Italic": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		48: [
			0,
			.43056,
			0,
			0,
			.5
		],
		49: [
			0,
			.43056,
			0,
			0,
			.5
		],
		50: [
			0,
			.43056,
			0,
			0,
			.5
		],
		51: [
			.19444,
			.43056,
			0,
			0,
			.5
		],
		52: [
			.19444,
			.43056,
			0,
			0,
			.5
		],
		53: [
			.19444,
			.43056,
			0,
			0,
			.5
		],
		54: [
			0,
			.64444,
			0,
			0,
			.5
		],
		55: [
			.19444,
			.43056,
			0,
			0,
			.5
		],
		56: [
			0,
			.64444,
			0,
			0,
			.5
		],
		57: [
			.19444,
			.43056,
			0,
			0,
			.5
		],
		65: [
			0,
			.68333,
			0,
			.13889,
			.75
		],
		66: [
			0,
			.68333,
			.05017,
			.08334,
			.75851
		],
		67: [
			0,
			.68333,
			.07153,
			.08334,
			.71472
		],
		68: [
			0,
			.68333,
			.02778,
			.05556,
			.82792
		],
		69: [
			0,
			.68333,
			.05764,
			.08334,
			.7382
		],
		70: [
			0,
			.68333,
			.13889,
			.08334,
			.64306
		],
		71: [
			0,
			.68333,
			0,
			.08334,
			.78625
		],
		72: [
			0,
			.68333,
			.08125,
			.05556,
			.83125
		],
		73: [
			0,
			.68333,
			.07847,
			.11111,
			.43958
		],
		74: [
			0,
			.68333,
			.09618,
			.16667,
			.55451
		],
		75: [
			0,
			.68333,
			.07153,
			.05556,
			.84931
		],
		76: [
			0,
			.68333,
			0,
			.02778,
			.68056
		],
		77: [
			0,
			.68333,
			.10903,
			.08334,
			.97014
		],
		78: [
			0,
			.68333,
			.10903,
			.08334,
			.80347
		],
		79: [
			0,
			.68333,
			.02778,
			.08334,
			.76278
		],
		80: [
			0,
			.68333,
			.13889,
			.08334,
			.64201
		],
		81: [
			.19444,
			.68333,
			0,
			.08334,
			.79056
		],
		82: [
			0,
			.68333,
			.00773,
			.08334,
			.75929
		],
		83: [
			0,
			.68333,
			.05764,
			.08334,
			.6132
		],
		84: [
			0,
			.68333,
			.13889,
			.08334,
			.58438
		],
		85: [
			0,
			.68333,
			.10903,
			.02778,
			.68278
		],
		86: [
			0,
			.68333,
			.22222,
			0,
			.58333
		],
		87: [
			0,
			.68333,
			.13889,
			0,
			.94445
		],
		88: [
			0,
			.68333,
			.07847,
			.08334,
			.82847
		],
		89: [
			0,
			.68333,
			.22222,
			0,
			.58056
		],
		90: [
			0,
			.68333,
			.07153,
			.08334,
			.68264
		],
		97: [
			0,
			.43056,
			0,
			0,
			.52859
		],
		98: [
			0,
			.69444,
			0,
			0,
			.42917
		],
		99: [
			0,
			.43056,
			0,
			.05556,
			.43276
		],
		100: [
			0,
			.69444,
			0,
			.16667,
			.52049
		],
		101: [
			0,
			.43056,
			0,
			.05556,
			.46563
		],
		102: [
			.19444,
			.69444,
			.10764,
			.16667,
			.48959
		],
		103: [
			.19444,
			.43056,
			.03588,
			.02778,
			.47697
		],
		104: [
			0,
			.69444,
			0,
			0,
			.57616
		],
		105: [
			0,
			.65952,
			0,
			0,
			.34451
		],
		106: [
			.19444,
			.65952,
			.05724,
			0,
			.41181
		],
		107: [
			0,
			.69444,
			.03148,
			0,
			.5206
		],
		108: [
			0,
			.69444,
			.01968,
			.08334,
			.29838
		],
		109: [
			0,
			.43056,
			0,
			0,
			.87801
		],
		110: [
			0,
			.43056,
			0,
			0,
			.60023
		],
		111: [
			0,
			.43056,
			0,
			.05556,
			.48472
		],
		112: [
			.19444,
			.43056,
			0,
			.08334,
			.50313
		],
		113: [
			.19444,
			.43056,
			.03588,
			.08334,
			.44641
		],
		114: [
			0,
			.43056,
			.02778,
			.05556,
			.45116
		],
		115: [
			0,
			.43056,
			0,
			.05556,
			.46875
		],
		116: [
			0,
			.61508,
			0,
			.08334,
			.36111
		],
		117: [
			0,
			.43056,
			0,
			.02778,
			.57246
		],
		118: [
			0,
			.43056,
			.03588,
			.02778,
			.48472
		],
		119: [
			0,
			.43056,
			.02691,
			.08334,
			.71592
		],
		120: [
			0,
			.43056,
			0,
			.02778,
			.57153
		],
		121: [
			.19444,
			.43056,
			.03588,
			.05556,
			.49028
		],
		122: [
			0,
			.43056,
			.04398,
			.05556,
			.46505
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		915: [
			0,
			.68333,
			.13889,
			.08334,
			.61528
		],
		916: [
			0,
			.68333,
			0,
			.16667,
			.83334
		],
		920: [
			0,
			.68333,
			.02778,
			.08334,
			.76278
		],
		923: [
			0,
			.68333,
			0,
			.16667,
			.69445
		],
		926: [
			0,
			.68333,
			.07569,
			.08334,
			.74236
		],
		928: [
			0,
			.68333,
			.08125,
			.05556,
			.83125
		],
		931: [
			0,
			.68333,
			.05764,
			.08334,
			.77986
		],
		933: [
			0,
			.68333,
			.13889,
			.05556,
			.58333
		],
		934: [
			0,
			.68333,
			0,
			.08334,
			.66667
		],
		936: [
			0,
			.68333,
			.11,
			.05556,
			.61222
		],
		937: [
			0,
			.68333,
			.05017,
			.08334,
			.7724
		],
		945: [
			0,
			.43056,
			.0037,
			.02778,
			.6397
		],
		946: [
			.19444,
			.69444,
			.05278,
			.08334,
			.56563
		],
		947: [
			.19444,
			.43056,
			.05556,
			0,
			.51773
		],
		948: [
			0,
			.69444,
			.03785,
			.05556,
			.44444
		],
		949: [
			0,
			.43056,
			0,
			.08334,
			.46632
		],
		950: [
			.19444,
			.69444,
			.07378,
			.08334,
			.4375
		],
		951: [
			.19444,
			.43056,
			.03588,
			.05556,
			.49653
		],
		952: [
			0,
			.69444,
			.02778,
			.08334,
			.46944
		],
		953: [
			0,
			.43056,
			0,
			.05556,
			.35394
		],
		954: [
			0,
			.43056,
			0,
			0,
			.57616
		],
		955: [
			0,
			.69444,
			0,
			0,
			.58334
		],
		956: [
			.19444,
			.43056,
			0,
			.02778,
			.60255
		],
		957: [
			0,
			.43056,
			.06366,
			.02778,
			.49398
		],
		958: [
			.19444,
			.69444,
			.04601,
			.11111,
			.4375
		],
		959: [
			0,
			.43056,
			0,
			.05556,
			.48472
		],
		960: [
			0,
			.43056,
			.03588,
			0,
			.57003
		],
		961: [
			.19444,
			.43056,
			0,
			.08334,
			.51702
		],
		962: [
			.09722,
			.43056,
			.07986,
			.08334,
			.36285
		],
		963: [
			0,
			.43056,
			.03588,
			0,
			.57141
		],
		964: [
			0,
			.43056,
			.1132,
			.02778,
			.43715
		],
		965: [
			0,
			.43056,
			.03588,
			.02778,
			.54028
		],
		966: [
			.19444,
			.43056,
			0,
			.08334,
			.65417
		],
		967: [
			.19444,
			.43056,
			0,
			.05556,
			.62569
		],
		968: [
			.19444,
			.69444,
			.03588,
			.11111,
			.65139
		],
		969: [
			0,
			.43056,
			.03588,
			0,
			.62245
		],
		977: [
			0,
			.69444,
			0,
			.08334,
			.59144
		],
		981: [
			.19444,
			.69444,
			0,
			.08334,
			.59583
		],
		982: [
			0,
			.43056,
			.02778,
			0,
			.82813
		],
		1009: [
			.19444,
			.43056,
			0,
			.08334,
			.51702
		],
		1013: [
			0,
			.43056,
			0,
			.05556,
			.4059
		],
		57649: [
			0,
			.43056,
			0,
			.02778,
			.32246
		],
		57911: [
			.19444,
			.43056,
			0,
			.08334,
			.38403
		]
	},
	"SansSerif-Bold": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		33: [
			0,
			.69444,
			0,
			0,
			.36667
		],
		34: [
			0,
			.69444,
			0,
			0,
			.55834
		],
		35: [
			.19444,
			.69444,
			0,
			0,
			.91667
		],
		36: [
			.05556,
			.75,
			0,
			0,
			.55
		],
		37: [
			.05556,
			.75,
			0,
			0,
			1.02912
		],
		38: [
			0,
			.69444,
			0,
			0,
			.83056
		],
		39: [
			0,
			.69444,
			0,
			0,
			.30556
		],
		40: [
			.25,
			.75,
			0,
			0,
			.42778
		],
		41: [
			.25,
			.75,
			0,
			0,
			.42778
		],
		42: [
			0,
			.75,
			0,
			0,
			.55
		],
		43: [
			.11667,
			.61667,
			0,
			0,
			.85556
		],
		44: [
			.10556,
			.13056,
			0,
			0,
			.30556
		],
		45: [
			0,
			.45833,
			0,
			0,
			.36667
		],
		46: [
			0,
			.13056,
			0,
			0,
			.30556
		],
		47: [
			.25,
			.75,
			0,
			0,
			.55
		],
		48: [
			0,
			.69444,
			0,
			0,
			.55
		],
		49: [
			0,
			.69444,
			0,
			0,
			.55
		],
		50: [
			0,
			.69444,
			0,
			0,
			.55
		],
		51: [
			0,
			.69444,
			0,
			0,
			.55
		],
		52: [
			0,
			.69444,
			0,
			0,
			.55
		],
		53: [
			0,
			.69444,
			0,
			0,
			.55
		],
		54: [
			0,
			.69444,
			0,
			0,
			.55
		],
		55: [
			0,
			.69444,
			0,
			0,
			.55
		],
		56: [
			0,
			.69444,
			0,
			0,
			.55
		],
		57: [
			0,
			.69444,
			0,
			0,
			.55
		],
		58: [
			0,
			.45833,
			0,
			0,
			.30556
		],
		59: [
			.10556,
			.45833,
			0,
			0,
			.30556
		],
		61: [
			-.09375,
			.40625,
			0,
			0,
			.85556
		],
		63: [
			0,
			.69444,
			0,
			0,
			.51945
		],
		64: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		65: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		66: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		67: [
			0,
			.69444,
			0,
			0,
			.70278
		],
		68: [
			0,
			.69444,
			0,
			0,
			.79445
		],
		69: [
			0,
			.69444,
			0,
			0,
			.64167
		],
		70: [
			0,
			.69444,
			0,
			0,
			.61111
		],
		71: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		72: [
			0,
			.69444,
			0,
			0,
			.79445
		],
		73: [
			0,
			.69444,
			0,
			0,
			.33056
		],
		74: [
			0,
			.69444,
			0,
			0,
			.51945
		],
		75: [
			0,
			.69444,
			0,
			0,
			.76389
		],
		76: [
			0,
			.69444,
			0,
			0,
			.58056
		],
		77: [
			0,
			.69444,
			0,
			0,
			.97778
		],
		78: [
			0,
			.69444,
			0,
			0,
			.79445
		],
		79: [
			0,
			.69444,
			0,
			0,
			.79445
		],
		80: [
			0,
			.69444,
			0,
			0,
			.70278
		],
		81: [
			.10556,
			.69444,
			0,
			0,
			.79445
		],
		82: [
			0,
			.69444,
			0,
			0,
			.70278
		],
		83: [
			0,
			.69444,
			0,
			0,
			.61111
		],
		84: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		85: [
			0,
			.69444,
			0,
			0,
			.76389
		],
		86: [
			0,
			.69444,
			.01528,
			0,
			.73334
		],
		87: [
			0,
			.69444,
			.01528,
			0,
			1.03889
		],
		88: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		89: [
			0,
			.69444,
			.0275,
			0,
			.73334
		],
		90: [
			0,
			.69444,
			0,
			0,
			.67223
		],
		91: [
			.25,
			.75,
			0,
			0,
			.34306
		],
		93: [
			.25,
			.75,
			0,
			0,
			.34306
		],
		94: [
			0,
			.69444,
			0,
			0,
			.55
		],
		95: [
			.35,
			.10833,
			.03056,
			0,
			.55
		],
		97: [
			0,
			.45833,
			0,
			0,
			.525
		],
		98: [
			0,
			.69444,
			0,
			0,
			.56111
		],
		99: [
			0,
			.45833,
			0,
			0,
			.48889
		],
		100: [
			0,
			.69444,
			0,
			0,
			.56111
		],
		101: [
			0,
			.45833,
			0,
			0,
			.51111
		],
		102: [
			0,
			.69444,
			.07639,
			0,
			.33611
		],
		103: [
			.19444,
			.45833,
			.01528,
			0,
			.55
		],
		104: [
			0,
			.69444,
			0,
			0,
			.56111
		],
		105: [
			0,
			.69444,
			0,
			0,
			.25556
		],
		106: [
			.19444,
			.69444,
			0,
			0,
			.28611
		],
		107: [
			0,
			.69444,
			0,
			0,
			.53056
		],
		108: [
			0,
			.69444,
			0,
			0,
			.25556
		],
		109: [
			0,
			.45833,
			0,
			0,
			.86667
		],
		110: [
			0,
			.45833,
			0,
			0,
			.56111
		],
		111: [
			0,
			.45833,
			0,
			0,
			.55
		],
		112: [
			.19444,
			.45833,
			0,
			0,
			.56111
		],
		113: [
			.19444,
			.45833,
			0,
			0,
			.56111
		],
		114: [
			0,
			.45833,
			.01528,
			0,
			.37222
		],
		115: [
			0,
			.45833,
			0,
			0,
			.42167
		],
		116: [
			0,
			.58929,
			0,
			0,
			.40417
		],
		117: [
			0,
			.45833,
			0,
			0,
			.56111
		],
		118: [
			0,
			.45833,
			.01528,
			0,
			.5
		],
		119: [
			0,
			.45833,
			.01528,
			0,
			.74445
		],
		120: [
			0,
			.45833,
			0,
			0,
			.5
		],
		121: [
			.19444,
			.45833,
			.01528,
			0,
			.5
		],
		122: [
			0,
			.45833,
			0,
			0,
			.47639
		],
		126: [
			.35,
			.34444,
			0,
			0,
			.55
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		168: [
			0,
			.69444,
			0,
			0,
			.55
		],
		176: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		180: [
			0,
			.69444,
			0,
			0,
			.55
		],
		184: [
			.17014,
			0,
			0,
			0,
			.48889
		],
		305: [
			0,
			.45833,
			0,
			0,
			.25556
		],
		567: [
			.19444,
			.45833,
			0,
			0,
			.28611
		],
		710: [
			0,
			.69444,
			0,
			0,
			.55
		],
		711: [
			0,
			.63542,
			0,
			0,
			.55
		],
		713: [
			0,
			.63778,
			0,
			0,
			.55
		],
		728: [
			0,
			.69444,
			0,
			0,
			.55
		],
		729: [
			0,
			.69444,
			0,
			0,
			.30556
		],
		730: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		732: [
			0,
			.69444,
			0,
			0,
			.55
		],
		733: [
			0,
			.69444,
			0,
			0,
			.55
		],
		915: [
			0,
			.69444,
			0,
			0,
			.58056
		],
		916: [
			0,
			.69444,
			0,
			0,
			.91667
		],
		920: [
			0,
			.69444,
			0,
			0,
			.85556
		],
		923: [
			0,
			.69444,
			0,
			0,
			.67223
		],
		926: [
			0,
			.69444,
			0,
			0,
			.73334
		],
		928: [
			0,
			.69444,
			0,
			0,
			.79445
		],
		931: [
			0,
			.69444,
			0,
			0,
			.79445
		],
		933: [
			0,
			.69444,
			0,
			0,
			.85556
		],
		934: [
			0,
			.69444,
			0,
			0,
			.79445
		],
		936: [
			0,
			.69444,
			0,
			0,
			.85556
		],
		937: [
			0,
			.69444,
			0,
			0,
			.79445
		],
		8211: [
			0,
			.45833,
			.03056,
			0,
			.55
		],
		8212: [
			0,
			.45833,
			.03056,
			0,
			1.10001
		],
		8216: [
			0,
			.69444,
			0,
			0,
			.30556
		],
		8217: [
			0,
			.69444,
			0,
			0,
			.30556
		],
		8220: [
			0,
			.69444,
			0,
			0,
			.55834
		],
		8221: [
			0,
			.69444,
			0,
			0,
			.55834
		]
	},
	"SansSerif-Italic": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		33: [
			0,
			.69444,
			.05733,
			0,
			.31945
		],
		34: [
			0,
			.69444,
			.00316,
			0,
			.5
		],
		35: [
			.19444,
			.69444,
			.05087,
			0,
			.83334
		],
		36: [
			.05556,
			.75,
			.11156,
			0,
			.5
		],
		37: [
			.05556,
			.75,
			.03126,
			0,
			.83334
		],
		38: [
			0,
			.69444,
			.03058,
			0,
			.75834
		],
		39: [
			0,
			.69444,
			.07816,
			0,
			.27778
		],
		40: [
			.25,
			.75,
			.13164,
			0,
			.38889
		],
		41: [
			.25,
			.75,
			.02536,
			0,
			.38889
		],
		42: [
			0,
			.75,
			.11775,
			0,
			.5
		],
		43: [
			.08333,
			.58333,
			.02536,
			0,
			.77778
		],
		44: [
			.125,
			.08333,
			0,
			0,
			.27778
		],
		45: [
			0,
			.44444,
			.01946,
			0,
			.33333
		],
		46: [
			0,
			.08333,
			0,
			0,
			.27778
		],
		47: [
			.25,
			.75,
			.13164,
			0,
			.5
		],
		48: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		49: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		50: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		51: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		52: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		53: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		54: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		55: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		56: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		57: [
			0,
			.65556,
			.11156,
			0,
			.5
		],
		58: [
			0,
			.44444,
			.02502,
			0,
			.27778
		],
		59: [
			.125,
			.44444,
			.02502,
			0,
			.27778
		],
		61: [
			-.13,
			.37,
			.05087,
			0,
			.77778
		],
		63: [
			0,
			.69444,
			.11809,
			0,
			.47222
		],
		64: [
			0,
			.69444,
			.07555,
			0,
			.66667
		],
		65: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		66: [
			0,
			.69444,
			.08293,
			0,
			.66667
		],
		67: [
			0,
			.69444,
			.11983,
			0,
			.63889
		],
		68: [
			0,
			.69444,
			.07555,
			0,
			.72223
		],
		69: [
			0,
			.69444,
			.11983,
			0,
			.59722
		],
		70: [
			0,
			.69444,
			.13372,
			0,
			.56945
		],
		71: [
			0,
			.69444,
			.11983,
			0,
			.66667
		],
		72: [
			0,
			.69444,
			.08094,
			0,
			.70834
		],
		73: [
			0,
			.69444,
			.13372,
			0,
			.27778
		],
		74: [
			0,
			.69444,
			.08094,
			0,
			.47222
		],
		75: [
			0,
			.69444,
			.11983,
			0,
			.69445
		],
		76: [
			0,
			.69444,
			0,
			0,
			.54167
		],
		77: [
			0,
			.69444,
			.08094,
			0,
			.875
		],
		78: [
			0,
			.69444,
			.08094,
			0,
			.70834
		],
		79: [
			0,
			.69444,
			.07555,
			0,
			.73611
		],
		80: [
			0,
			.69444,
			.08293,
			0,
			.63889
		],
		81: [
			.125,
			.69444,
			.07555,
			0,
			.73611
		],
		82: [
			0,
			.69444,
			.08293,
			0,
			.64584
		],
		83: [
			0,
			.69444,
			.09205,
			0,
			.55556
		],
		84: [
			0,
			.69444,
			.13372,
			0,
			.68056
		],
		85: [
			0,
			.69444,
			.08094,
			0,
			.6875
		],
		86: [
			0,
			.69444,
			.1615,
			0,
			.66667
		],
		87: [
			0,
			.69444,
			.1615,
			0,
			.94445
		],
		88: [
			0,
			.69444,
			.13372,
			0,
			.66667
		],
		89: [
			0,
			.69444,
			.17261,
			0,
			.66667
		],
		90: [
			0,
			.69444,
			.11983,
			0,
			.61111
		],
		91: [
			.25,
			.75,
			.15942,
			0,
			.28889
		],
		93: [
			.25,
			.75,
			.08719,
			0,
			.28889
		],
		94: [
			0,
			.69444,
			.0799,
			0,
			.5
		],
		95: [
			.35,
			.09444,
			.08616,
			0,
			.5
		],
		97: [
			0,
			.44444,
			.00981,
			0,
			.48056
		],
		98: [
			0,
			.69444,
			.03057,
			0,
			.51667
		],
		99: [
			0,
			.44444,
			.08336,
			0,
			.44445
		],
		100: [
			0,
			.69444,
			.09483,
			0,
			.51667
		],
		101: [
			0,
			.44444,
			.06778,
			0,
			.44445
		],
		102: [
			0,
			.69444,
			.21705,
			0,
			.30556
		],
		103: [
			.19444,
			.44444,
			.10836,
			0,
			.5
		],
		104: [
			0,
			.69444,
			.01778,
			0,
			.51667
		],
		105: [
			0,
			.67937,
			.09718,
			0,
			.23889
		],
		106: [
			.19444,
			.67937,
			.09162,
			0,
			.26667
		],
		107: [
			0,
			.69444,
			.08336,
			0,
			.48889
		],
		108: [
			0,
			.69444,
			.09483,
			0,
			.23889
		],
		109: [
			0,
			.44444,
			.01778,
			0,
			.79445
		],
		110: [
			0,
			.44444,
			.01778,
			0,
			.51667
		],
		111: [
			0,
			.44444,
			.06613,
			0,
			.5
		],
		112: [
			.19444,
			.44444,
			.0389,
			0,
			.51667
		],
		113: [
			.19444,
			.44444,
			.04169,
			0,
			.51667
		],
		114: [
			0,
			.44444,
			.10836,
			0,
			.34167
		],
		115: [
			0,
			.44444,
			.0778,
			0,
			.38333
		],
		116: [
			0,
			.57143,
			.07225,
			0,
			.36111
		],
		117: [
			0,
			.44444,
			.04169,
			0,
			.51667
		],
		118: [
			0,
			.44444,
			.10836,
			0,
			.46111
		],
		119: [
			0,
			.44444,
			.10836,
			0,
			.68334
		],
		120: [
			0,
			.44444,
			.09169,
			0,
			.46111
		],
		121: [
			.19444,
			.44444,
			.10836,
			0,
			.46111
		],
		122: [
			0,
			.44444,
			.08752,
			0,
			.43472
		],
		126: [
			.35,
			.32659,
			.08826,
			0,
			.5
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		168: [
			0,
			.67937,
			.06385,
			0,
			.5
		],
		176: [
			0,
			.69444,
			0,
			0,
			.73752
		],
		184: [
			.17014,
			0,
			0,
			0,
			.44445
		],
		305: [
			0,
			.44444,
			.04169,
			0,
			.23889
		],
		567: [
			.19444,
			.44444,
			.04169,
			0,
			.26667
		],
		710: [
			0,
			.69444,
			.0799,
			0,
			.5
		],
		711: [
			0,
			.63194,
			.08432,
			0,
			.5
		],
		713: [
			0,
			.60889,
			.08776,
			0,
			.5
		],
		714: [
			0,
			.69444,
			.09205,
			0,
			.5
		],
		715: [
			0,
			.69444,
			0,
			0,
			.5
		],
		728: [
			0,
			.69444,
			.09483,
			0,
			.5
		],
		729: [
			0,
			.67937,
			.07774,
			0,
			.27778
		],
		730: [
			0,
			.69444,
			0,
			0,
			.73752
		],
		732: [
			0,
			.67659,
			.08826,
			0,
			.5
		],
		733: [
			0,
			.69444,
			.09205,
			0,
			.5
		],
		915: [
			0,
			.69444,
			.13372,
			0,
			.54167
		],
		916: [
			0,
			.69444,
			0,
			0,
			.83334
		],
		920: [
			0,
			.69444,
			.07555,
			0,
			.77778
		],
		923: [
			0,
			.69444,
			0,
			0,
			.61111
		],
		926: [
			0,
			.69444,
			.12816,
			0,
			.66667
		],
		928: [
			0,
			.69444,
			.08094,
			0,
			.70834
		],
		931: [
			0,
			.69444,
			.11983,
			0,
			.72222
		],
		933: [
			0,
			.69444,
			.09031,
			0,
			.77778
		],
		934: [
			0,
			.69444,
			.04603,
			0,
			.72222
		],
		936: [
			0,
			.69444,
			.09031,
			0,
			.77778
		],
		937: [
			0,
			.69444,
			.08293,
			0,
			.72222
		],
		8211: [
			0,
			.44444,
			.08616,
			0,
			.5
		],
		8212: [
			0,
			.44444,
			.08616,
			0,
			1
		],
		8216: [
			0,
			.69444,
			.07816,
			0,
			.27778
		],
		8217: [
			0,
			.69444,
			.07816,
			0,
			.27778
		],
		8220: [
			0,
			.69444,
			.14205,
			0,
			.5
		],
		8221: [
			0,
			.69444,
			.00316,
			0,
			.5
		]
	},
	"SansSerif-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		33: [
			0,
			.69444,
			0,
			0,
			.31945
		],
		34: [
			0,
			.69444,
			0,
			0,
			.5
		],
		35: [
			.19444,
			.69444,
			0,
			0,
			.83334
		],
		36: [
			.05556,
			.75,
			0,
			0,
			.5
		],
		37: [
			.05556,
			.75,
			0,
			0,
			.83334
		],
		38: [
			0,
			.69444,
			0,
			0,
			.75834
		],
		39: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		40: [
			.25,
			.75,
			0,
			0,
			.38889
		],
		41: [
			.25,
			.75,
			0,
			0,
			.38889
		],
		42: [
			0,
			.75,
			0,
			0,
			.5
		],
		43: [
			.08333,
			.58333,
			0,
			0,
			.77778
		],
		44: [
			.125,
			.08333,
			0,
			0,
			.27778
		],
		45: [
			0,
			.44444,
			0,
			0,
			.33333
		],
		46: [
			0,
			.08333,
			0,
			0,
			.27778
		],
		47: [
			.25,
			.75,
			0,
			0,
			.5
		],
		48: [
			0,
			.65556,
			0,
			0,
			.5
		],
		49: [
			0,
			.65556,
			0,
			0,
			.5
		],
		50: [
			0,
			.65556,
			0,
			0,
			.5
		],
		51: [
			0,
			.65556,
			0,
			0,
			.5
		],
		52: [
			0,
			.65556,
			0,
			0,
			.5
		],
		53: [
			0,
			.65556,
			0,
			0,
			.5
		],
		54: [
			0,
			.65556,
			0,
			0,
			.5
		],
		55: [
			0,
			.65556,
			0,
			0,
			.5
		],
		56: [
			0,
			.65556,
			0,
			0,
			.5
		],
		57: [
			0,
			.65556,
			0,
			0,
			.5
		],
		58: [
			0,
			.44444,
			0,
			0,
			.27778
		],
		59: [
			.125,
			.44444,
			0,
			0,
			.27778
		],
		61: [
			-.13,
			.37,
			0,
			0,
			.77778
		],
		63: [
			0,
			.69444,
			0,
			0,
			.47222
		],
		64: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		65: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		66: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		67: [
			0,
			.69444,
			0,
			0,
			.63889
		],
		68: [
			0,
			.69444,
			0,
			0,
			.72223
		],
		69: [
			0,
			.69444,
			0,
			0,
			.59722
		],
		70: [
			0,
			.69444,
			0,
			0,
			.56945
		],
		71: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		72: [
			0,
			.69444,
			0,
			0,
			.70834
		],
		73: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		74: [
			0,
			.69444,
			0,
			0,
			.47222
		],
		75: [
			0,
			.69444,
			0,
			0,
			.69445
		],
		76: [
			0,
			.69444,
			0,
			0,
			.54167
		],
		77: [
			0,
			.69444,
			0,
			0,
			.875
		],
		78: [
			0,
			.69444,
			0,
			0,
			.70834
		],
		79: [
			0,
			.69444,
			0,
			0,
			.73611
		],
		80: [
			0,
			.69444,
			0,
			0,
			.63889
		],
		81: [
			.125,
			.69444,
			0,
			0,
			.73611
		],
		82: [
			0,
			.69444,
			0,
			0,
			.64584
		],
		83: [
			0,
			.69444,
			0,
			0,
			.55556
		],
		84: [
			0,
			.69444,
			0,
			0,
			.68056
		],
		85: [
			0,
			.69444,
			0,
			0,
			.6875
		],
		86: [
			0,
			.69444,
			.01389,
			0,
			.66667
		],
		87: [
			0,
			.69444,
			.01389,
			0,
			.94445
		],
		88: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		89: [
			0,
			.69444,
			.025,
			0,
			.66667
		],
		90: [
			0,
			.69444,
			0,
			0,
			.61111
		],
		91: [
			.25,
			.75,
			0,
			0,
			.28889
		],
		93: [
			.25,
			.75,
			0,
			0,
			.28889
		],
		94: [
			0,
			.69444,
			0,
			0,
			.5
		],
		95: [
			.35,
			.09444,
			.02778,
			0,
			.5
		],
		97: [
			0,
			.44444,
			0,
			0,
			.48056
		],
		98: [
			0,
			.69444,
			0,
			0,
			.51667
		],
		99: [
			0,
			.44444,
			0,
			0,
			.44445
		],
		100: [
			0,
			.69444,
			0,
			0,
			.51667
		],
		101: [
			0,
			.44444,
			0,
			0,
			.44445
		],
		102: [
			0,
			.69444,
			.06944,
			0,
			.30556
		],
		103: [
			.19444,
			.44444,
			.01389,
			0,
			.5
		],
		104: [
			0,
			.69444,
			0,
			0,
			.51667
		],
		105: [
			0,
			.67937,
			0,
			0,
			.23889
		],
		106: [
			.19444,
			.67937,
			0,
			0,
			.26667
		],
		107: [
			0,
			.69444,
			0,
			0,
			.48889
		],
		108: [
			0,
			.69444,
			0,
			0,
			.23889
		],
		109: [
			0,
			.44444,
			0,
			0,
			.79445
		],
		110: [
			0,
			.44444,
			0,
			0,
			.51667
		],
		111: [
			0,
			.44444,
			0,
			0,
			.5
		],
		112: [
			.19444,
			.44444,
			0,
			0,
			.51667
		],
		113: [
			.19444,
			.44444,
			0,
			0,
			.51667
		],
		114: [
			0,
			.44444,
			.01389,
			0,
			.34167
		],
		115: [
			0,
			.44444,
			0,
			0,
			.38333
		],
		116: [
			0,
			.57143,
			0,
			0,
			.36111
		],
		117: [
			0,
			.44444,
			0,
			0,
			.51667
		],
		118: [
			0,
			.44444,
			.01389,
			0,
			.46111
		],
		119: [
			0,
			.44444,
			.01389,
			0,
			.68334
		],
		120: [
			0,
			.44444,
			0,
			0,
			.46111
		],
		121: [
			.19444,
			.44444,
			.01389,
			0,
			.46111
		],
		122: [
			0,
			.44444,
			0,
			0,
			.43472
		],
		126: [
			.35,
			.32659,
			0,
			0,
			.5
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		168: [
			0,
			.67937,
			0,
			0,
			.5
		],
		176: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		184: [
			.17014,
			0,
			0,
			0,
			.44445
		],
		305: [
			0,
			.44444,
			0,
			0,
			.23889
		],
		567: [
			.19444,
			.44444,
			0,
			0,
			.26667
		],
		710: [
			0,
			.69444,
			0,
			0,
			.5
		],
		711: [
			0,
			.63194,
			0,
			0,
			.5
		],
		713: [
			0,
			.60889,
			0,
			0,
			.5
		],
		714: [
			0,
			.69444,
			0,
			0,
			.5
		],
		715: [
			0,
			.69444,
			0,
			0,
			.5
		],
		728: [
			0,
			.69444,
			0,
			0,
			.5
		],
		729: [
			0,
			.67937,
			0,
			0,
			.27778
		],
		730: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		732: [
			0,
			.67659,
			0,
			0,
			.5
		],
		733: [
			0,
			.69444,
			0,
			0,
			.5
		],
		915: [
			0,
			.69444,
			0,
			0,
			.54167
		],
		916: [
			0,
			.69444,
			0,
			0,
			.83334
		],
		920: [
			0,
			.69444,
			0,
			0,
			.77778
		],
		923: [
			0,
			.69444,
			0,
			0,
			.61111
		],
		926: [
			0,
			.69444,
			0,
			0,
			.66667
		],
		928: [
			0,
			.69444,
			0,
			0,
			.70834
		],
		931: [
			0,
			.69444,
			0,
			0,
			.72222
		],
		933: [
			0,
			.69444,
			0,
			0,
			.77778
		],
		934: [
			0,
			.69444,
			0,
			0,
			.72222
		],
		936: [
			0,
			.69444,
			0,
			0,
			.77778
		],
		937: [
			0,
			.69444,
			0,
			0,
			.72222
		],
		8211: [
			0,
			.44444,
			.02778,
			0,
			.5
		],
		8212: [
			0,
			.44444,
			.02778,
			0,
			1
		],
		8216: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		8217: [
			0,
			.69444,
			0,
			0,
			.27778
		],
		8220: [
			0,
			.69444,
			0,
			0,
			.5
		],
		8221: [
			0,
			.69444,
			0,
			0,
			.5
		]
	},
	"Script-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		65: [
			0,
			.7,
			.22925,
			0,
			.80253
		],
		66: [
			0,
			.7,
			.04087,
			0,
			.90757
		],
		67: [
			0,
			.7,
			.1689,
			0,
			.66619
		],
		68: [
			0,
			.7,
			.09371,
			0,
			.77443
		],
		69: [
			0,
			.7,
			.18583,
			0,
			.56162
		],
		70: [
			0,
			.7,
			.13634,
			0,
			.89544
		],
		71: [
			0,
			.7,
			.17322,
			0,
			.60961
		],
		72: [
			0,
			.7,
			.29694,
			0,
			.96919
		],
		73: [
			0,
			.7,
			.19189,
			0,
			.80907
		],
		74: [
			.27778,
			.7,
			.19189,
			0,
			1.05159
		],
		75: [
			0,
			.7,
			.31259,
			0,
			.91364
		],
		76: [
			0,
			.7,
			.19189,
			0,
			.87373
		],
		77: [
			0,
			.7,
			.15981,
			0,
			1.08031
		],
		78: [
			0,
			.7,
			.3525,
			0,
			.9015
		],
		79: [
			0,
			.7,
			.08078,
			0,
			.73787
		],
		80: [
			0,
			.7,
			.08078,
			0,
			1.01262
		],
		81: [
			0,
			.7,
			.03305,
			0,
			.88282
		],
		82: [
			0,
			.7,
			.06259,
			0,
			.85
		],
		83: [
			0,
			.7,
			.19189,
			0,
			.86767
		],
		84: [
			0,
			.7,
			.29087,
			0,
			.74697
		],
		85: [
			0,
			.7,
			.25815,
			0,
			.79996
		],
		86: [
			0,
			.7,
			.27523,
			0,
			.62204
		],
		87: [
			0,
			.7,
			.27523,
			0,
			.80532
		],
		88: [
			0,
			.7,
			.26006,
			0,
			.94445
		],
		89: [
			0,
			.7,
			.2939,
			0,
			.70961
		],
		90: [
			0,
			.7,
			.24037,
			0,
			.8212
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		]
	},
	"Size1-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		40: [
			.35001,
			.85,
			0,
			0,
			.45834
		],
		41: [
			.35001,
			.85,
			0,
			0,
			.45834
		],
		47: [
			.35001,
			.85,
			0,
			0,
			.57778
		],
		91: [
			.35001,
			.85,
			0,
			0,
			.41667
		],
		92: [
			.35001,
			.85,
			0,
			0,
			.57778
		],
		93: [
			.35001,
			.85,
			0,
			0,
			.41667
		],
		123: [
			.35001,
			.85,
			0,
			0,
			.58334
		],
		125: [
			.35001,
			.85,
			0,
			0,
			.58334
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		710: [
			0,
			.72222,
			0,
			0,
			.55556
		],
		732: [
			0,
			.72222,
			0,
			0,
			.55556
		],
		770: [
			0,
			.72222,
			0,
			0,
			.55556
		],
		771: [
			0,
			.72222,
			0,
			0,
			.55556
		],
		8214: [
			-99e-5,
			.601,
			0,
			0,
			.77778
		],
		8593: [
			1e-5,
			.6,
			0,
			0,
			.66667
		],
		8595: [
			1e-5,
			.6,
			0,
			0,
			.66667
		],
		8657: [
			1e-5,
			.6,
			0,
			0,
			.77778
		],
		8659: [
			1e-5,
			.6,
			0,
			0,
			.77778
		],
		8719: [
			.25001,
			.75,
			0,
			0,
			.94445
		],
		8720: [
			.25001,
			.75,
			0,
			0,
			.94445
		],
		8721: [
			.25001,
			.75,
			0,
			0,
			1.05556
		],
		8730: [
			.35001,
			.85,
			0,
			0,
			1
		],
		8739: [
			-.00599,
			.606,
			0,
			0,
			.33333
		],
		8741: [
			-.00599,
			.606,
			0,
			0,
			.55556
		],
		8747: [
			.30612,
			.805,
			.19445,
			0,
			.47222
		],
		8748: [
			.306,
			.805,
			.19445,
			0,
			.47222
		],
		8749: [
			.306,
			.805,
			.19445,
			0,
			.47222
		],
		8750: [
			.30612,
			.805,
			.19445,
			0,
			.47222
		],
		8896: [
			.25001,
			.75,
			0,
			0,
			.83334
		],
		8897: [
			.25001,
			.75,
			0,
			0,
			.83334
		],
		8898: [
			.25001,
			.75,
			0,
			0,
			.83334
		],
		8899: [
			.25001,
			.75,
			0,
			0,
			.83334
		],
		8968: [
			.35001,
			.85,
			0,
			0,
			.47222
		],
		8969: [
			.35001,
			.85,
			0,
			0,
			.47222
		],
		8970: [
			.35001,
			.85,
			0,
			0,
			.47222
		],
		8971: [
			.35001,
			.85,
			0,
			0,
			.47222
		],
		9168: [
			-99e-5,
			.601,
			0,
			0,
			.66667
		],
		10216: [
			.35001,
			.85,
			0,
			0,
			.47222
		],
		10217: [
			.35001,
			.85,
			0,
			0,
			.47222
		],
		10752: [
			.25001,
			.75,
			0,
			0,
			1.11111
		],
		10753: [
			.25001,
			.75,
			0,
			0,
			1.11111
		],
		10754: [
			.25001,
			.75,
			0,
			0,
			1.11111
		],
		10756: [
			.25001,
			.75,
			0,
			0,
			.83334
		],
		10758: [
			.25001,
			.75,
			0,
			0,
			.83334
		]
	},
	"Size2-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		40: [
			.65002,
			1.15,
			0,
			0,
			.59722
		],
		41: [
			.65002,
			1.15,
			0,
			0,
			.59722
		],
		47: [
			.65002,
			1.15,
			0,
			0,
			.81111
		],
		91: [
			.65002,
			1.15,
			0,
			0,
			.47222
		],
		92: [
			.65002,
			1.15,
			0,
			0,
			.81111
		],
		93: [
			.65002,
			1.15,
			0,
			0,
			.47222
		],
		123: [
			.65002,
			1.15,
			0,
			0,
			.66667
		],
		125: [
			.65002,
			1.15,
			0,
			0,
			.66667
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		710: [
			0,
			.75,
			0,
			0,
			1
		],
		732: [
			0,
			.75,
			0,
			0,
			1
		],
		770: [
			0,
			.75,
			0,
			0,
			1
		],
		771: [
			0,
			.75,
			0,
			0,
			1
		],
		8719: [
			.55001,
			1.05,
			0,
			0,
			1.27778
		],
		8720: [
			.55001,
			1.05,
			0,
			0,
			1.27778
		],
		8721: [
			.55001,
			1.05,
			0,
			0,
			1.44445
		],
		8730: [
			.65002,
			1.15,
			0,
			0,
			1
		],
		8747: [
			.86225,
			1.36,
			.44445,
			0,
			.55556
		],
		8748: [
			.862,
			1.36,
			.44445,
			0,
			.55556
		],
		8749: [
			.862,
			1.36,
			.44445,
			0,
			.55556
		],
		8750: [
			.86225,
			1.36,
			.44445,
			0,
			.55556
		],
		8896: [
			.55001,
			1.05,
			0,
			0,
			1.11111
		],
		8897: [
			.55001,
			1.05,
			0,
			0,
			1.11111
		],
		8898: [
			.55001,
			1.05,
			0,
			0,
			1.11111
		],
		8899: [
			.55001,
			1.05,
			0,
			0,
			1.11111
		],
		8968: [
			.65002,
			1.15,
			0,
			0,
			.52778
		],
		8969: [
			.65002,
			1.15,
			0,
			0,
			.52778
		],
		8970: [
			.65002,
			1.15,
			0,
			0,
			.52778
		],
		8971: [
			.65002,
			1.15,
			0,
			0,
			.52778
		],
		10216: [
			.65002,
			1.15,
			0,
			0,
			.61111
		],
		10217: [
			.65002,
			1.15,
			0,
			0,
			.61111
		],
		10752: [
			.55001,
			1.05,
			0,
			0,
			1.51112
		],
		10753: [
			.55001,
			1.05,
			0,
			0,
			1.51112
		],
		10754: [
			.55001,
			1.05,
			0,
			0,
			1.51112
		],
		10756: [
			.55001,
			1.05,
			0,
			0,
			1.11111
		],
		10758: [
			.55001,
			1.05,
			0,
			0,
			1.11111
		]
	},
	"Size3-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		40: [
			.95003,
			1.45,
			0,
			0,
			.73611
		],
		41: [
			.95003,
			1.45,
			0,
			0,
			.73611
		],
		47: [
			.95003,
			1.45,
			0,
			0,
			1.04445
		],
		91: [
			.95003,
			1.45,
			0,
			0,
			.52778
		],
		92: [
			.95003,
			1.45,
			0,
			0,
			1.04445
		],
		93: [
			.95003,
			1.45,
			0,
			0,
			.52778
		],
		123: [
			.95003,
			1.45,
			0,
			0,
			.75
		],
		125: [
			.95003,
			1.45,
			0,
			0,
			.75
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		710: [
			0,
			.75,
			0,
			0,
			1.44445
		],
		732: [
			0,
			.75,
			0,
			0,
			1.44445
		],
		770: [
			0,
			.75,
			0,
			0,
			1.44445
		],
		771: [
			0,
			.75,
			0,
			0,
			1.44445
		],
		8730: [
			.95003,
			1.45,
			0,
			0,
			1
		],
		8968: [
			.95003,
			1.45,
			0,
			0,
			.58334
		],
		8969: [
			.95003,
			1.45,
			0,
			0,
			.58334
		],
		8970: [
			.95003,
			1.45,
			0,
			0,
			.58334
		],
		8971: [
			.95003,
			1.45,
			0,
			0,
			.58334
		],
		10216: [
			.95003,
			1.45,
			0,
			0,
			.75
		],
		10217: [
			.95003,
			1.45,
			0,
			0,
			.75
		]
	},
	"Size4-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.25
		],
		40: [
			1.25003,
			1.75,
			0,
			0,
			.79167
		],
		41: [
			1.25003,
			1.75,
			0,
			0,
			.79167
		],
		47: [
			1.25003,
			1.75,
			0,
			0,
			1.27778
		],
		91: [
			1.25003,
			1.75,
			0,
			0,
			.58334
		],
		92: [
			1.25003,
			1.75,
			0,
			0,
			1.27778
		],
		93: [
			1.25003,
			1.75,
			0,
			0,
			.58334
		],
		123: [
			1.25003,
			1.75,
			0,
			0,
			.80556
		],
		125: [
			1.25003,
			1.75,
			0,
			0,
			.80556
		],
		160: [
			0,
			0,
			0,
			0,
			.25
		],
		710: [
			0,
			.825,
			0,
			0,
			1.8889
		],
		732: [
			0,
			.825,
			0,
			0,
			1.8889
		],
		770: [
			0,
			.825,
			0,
			0,
			1.8889
		],
		771: [
			0,
			.825,
			0,
			0,
			1.8889
		],
		8730: [
			1.25003,
			1.75,
			0,
			0,
			1
		],
		8968: [
			1.25003,
			1.75,
			0,
			0,
			.63889
		],
		8969: [
			1.25003,
			1.75,
			0,
			0,
			.63889
		],
		8970: [
			1.25003,
			1.75,
			0,
			0,
			.63889
		],
		8971: [
			1.25003,
			1.75,
			0,
			0,
			.63889
		],
		9115: [
			.64502,
			1.155,
			0,
			0,
			.875
		],
		9116: [
			1e-5,
			.6,
			0,
			0,
			.875
		],
		9117: [
			.64502,
			1.155,
			0,
			0,
			.875
		],
		9118: [
			.64502,
			1.155,
			0,
			0,
			.875
		],
		9119: [
			1e-5,
			.6,
			0,
			0,
			.875
		],
		9120: [
			.64502,
			1.155,
			0,
			0,
			.875
		],
		9121: [
			.64502,
			1.155,
			0,
			0,
			.66667
		],
		9122: [
			-99e-5,
			.601,
			0,
			0,
			.66667
		],
		9123: [
			.64502,
			1.155,
			0,
			0,
			.66667
		],
		9124: [
			.64502,
			1.155,
			0,
			0,
			.66667
		],
		9125: [
			-99e-5,
			.601,
			0,
			0,
			.66667
		],
		9126: [
			.64502,
			1.155,
			0,
			0,
			.66667
		],
		9127: [
			1e-5,
			.9,
			0,
			0,
			.88889
		],
		9128: [
			.65002,
			1.15,
			0,
			0,
			.88889
		],
		9129: [
			.90001,
			0,
			0,
			0,
			.88889
		],
		9130: [
			0,
			.3,
			0,
			0,
			.88889
		],
		9131: [
			1e-5,
			.9,
			0,
			0,
			.88889
		],
		9132: [
			.65002,
			1.15,
			0,
			0,
			.88889
		],
		9133: [
			.90001,
			0,
			0,
			0,
			.88889
		],
		9143: [
			.88502,
			.915,
			0,
			0,
			1.05556
		],
		10216: [
			1.25003,
			1.75,
			0,
			0,
			.80556
		],
		10217: [
			1.25003,
			1.75,
			0,
			0,
			.80556
		],
		57344: [
			-.00499,
			.605,
			0,
			0,
			1.05556
		],
		57345: [
			-.00499,
			.605,
			0,
			0,
			1.05556
		],
		57680: [
			0,
			.12,
			0,
			0,
			.45
		],
		57681: [
			0,
			.12,
			0,
			0,
			.45
		],
		57682: [
			0,
			.12,
			0,
			0,
			.45
		],
		57683: [
			0,
			.12,
			0,
			0,
			.45
		]
	},
	"Typewriter-Regular": {
		32: [
			0,
			0,
			0,
			0,
			.525
		],
		33: [
			0,
			.61111,
			0,
			0,
			.525
		],
		34: [
			0,
			.61111,
			0,
			0,
			.525
		],
		35: [
			0,
			.61111,
			0,
			0,
			.525
		],
		36: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		37: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		38: [
			0,
			.61111,
			0,
			0,
			.525
		],
		39: [
			0,
			.61111,
			0,
			0,
			.525
		],
		40: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		41: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		42: [
			0,
			.52083,
			0,
			0,
			.525
		],
		43: [
			-.08056,
			.53055,
			0,
			0,
			.525
		],
		44: [
			.13889,
			.125,
			0,
			0,
			.525
		],
		45: [
			-.08056,
			.53055,
			0,
			0,
			.525
		],
		46: [
			0,
			.125,
			0,
			0,
			.525
		],
		47: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		48: [
			0,
			.61111,
			0,
			0,
			.525
		],
		49: [
			0,
			.61111,
			0,
			0,
			.525
		],
		50: [
			0,
			.61111,
			0,
			0,
			.525
		],
		51: [
			0,
			.61111,
			0,
			0,
			.525
		],
		52: [
			0,
			.61111,
			0,
			0,
			.525
		],
		53: [
			0,
			.61111,
			0,
			0,
			.525
		],
		54: [
			0,
			.61111,
			0,
			0,
			.525
		],
		55: [
			0,
			.61111,
			0,
			0,
			.525
		],
		56: [
			0,
			.61111,
			0,
			0,
			.525
		],
		57: [
			0,
			.61111,
			0,
			0,
			.525
		],
		58: [
			0,
			.43056,
			0,
			0,
			.525
		],
		59: [
			.13889,
			.43056,
			0,
			0,
			.525
		],
		60: [
			-.05556,
			.55556,
			0,
			0,
			.525
		],
		61: [
			-.19549,
			.41562,
			0,
			0,
			.525
		],
		62: [
			-.05556,
			.55556,
			0,
			0,
			.525
		],
		63: [
			0,
			.61111,
			0,
			0,
			.525
		],
		64: [
			0,
			.61111,
			0,
			0,
			.525
		],
		65: [
			0,
			.61111,
			0,
			0,
			.525
		],
		66: [
			0,
			.61111,
			0,
			0,
			.525
		],
		67: [
			0,
			.61111,
			0,
			0,
			.525
		],
		68: [
			0,
			.61111,
			0,
			0,
			.525
		],
		69: [
			0,
			.61111,
			0,
			0,
			.525
		],
		70: [
			0,
			.61111,
			0,
			0,
			.525
		],
		71: [
			0,
			.61111,
			0,
			0,
			.525
		],
		72: [
			0,
			.61111,
			0,
			0,
			.525
		],
		73: [
			0,
			.61111,
			0,
			0,
			.525
		],
		74: [
			0,
			.61111,
			0,
			0,
			.525
		],
		75: [
			0,
			.61111,
			0,
			0,
			.525
		],
		76: [
			0,
			.61111,
			0,
			0,
			.525
		],
		77: [
			0,
			.61111,
			0,
			0,
			.525
		],
		78: [
			0,
			.61111,
			0,
			0,
			.525
		],
		79: [
			0,
			.61111,
			0,
			0,
			.525
		],
		80: [
			0,
			.61111,
			0,
			0,
			.525
		],
		81: [
			.13889,
			.61111,
			0,
			0,
			.525
		],
		82: [
			0,
			.61111,
			0,
			0,
			.525
		],
		83: [
			0,
			.61111,
			0,
			0,
			.525
		],
		84: [
			0,
			.61111,
			0,
			0,
			.525
		],
		85: [
			0,
			.61111,
			0,
			0,
			.525
		],
		86: [
			0,
			.61111,
			0,
			0,
			.525
		],
		87: [
			0,
			.61111,
			0,
			0,
			.525
		],
		88: [
			0,
			.61111,
			0,
			0,
			.525
		],
		89: [
			0,
			.61111,
			0,
			0,
			.525
		],
		90: [
			0,
			.61111,
			0,
			0,
			.525
		],
		91: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		92: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		93: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		94: [
			0,
			.61111,
			0,
			0,
			.525
		],
		95: [
			.09514,
			0,
			0,
			0,
			.525
		],
		96: [
			0,
			.61111,
			0,
			0,
			.525
		],
		97: [
			0,
			.43056,
			0,
			0,
			.525
		],
		98: [
			0,
			.61111,
			0,
			0,
			.525
		],
		99: [
			0,
			.43056,
			0,
			0,
			.525
		],
		100: [
			0,
			.61111,
			0,
			0,
			.525
		],
		101: [
			0,
			.43056,
			0,
			0,
			.525
		],
		102: [
			0,
			.61111,
			0,
			0,
			.525
		],
		103: [
			.22222,
			.43056,
			0,
			0,
			.525
		],
		104: [
			0,
			.61111,
			0,
			0,
			.525
		],
		105: [
			0,
			.61111,
			0,
			0,
			.525
		],
		106: [
			.22222,
			.61111,
			0,
			0,
			.525
		],
		107: [
			0,
			.61111,
			0,
			0,
			.525
		],
		108: [
			0,
			.61111,
			0,
			0,
			.525
		],
		109: [
			0,
			.43056,
			0,
			0,
			.525
		],
		110: [
			0,
			.43056,
			0,
			0,
			.525
		],
		111: [
			0,
			.43056,
			0,
			0,
			.525
		],
		112: [
			.22222,
			.43056,
			0,
			0,
			.525
		],
		113: [
			.22222,
			.43056,
			0,
			0,
			.525
		],
		114: [
			0,
			.43056,
			0,
			0,
			.525
		],
		115: [
			0,
			.43056,
			0,
			0,
			.525
		],
		116: [
			0,
			.55358,
			0,
			0,
			.525
		],
		117: [
			0,
			.43056,
			0,
			0,
			.525
		],
		118: [
			0,
			.43056,
			0,
			0,
			.525
		],
		119: [
			0,
			.43056,
			0,
			0,
			.525
		],
		120: [
			0,
			.43056,
			0,
			0,
			.525
		],
		121: [
			.22222,
			.43056,
			0,
			0,
			.525
		],
		122: [
			0,
			.43056,
			0,
			0,
			.525
		],
		123: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		124: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		125: [
			.08333,
			.69444,
			0,
			0,
			.525
		],
		126: [
			0,
			.61111,
			0,
			0,
			.525
		],
		127: [
			0,
			.61111,
			0,
			0,
			.525
		],
		160: [
			0,
			0,
			0,
			0,
			.525
		],
		176: [
			0,
			.61111,
			0,
			0,
			.525
		],
		184: [
			.19445,
			0,
			0,
			0,
			.525
		],
		305: [
			0,
			.43056,
			0,
			0,
			.525
		],
		567: [
			.22222,
			.43056,
			0,
			0,
			.525
		],
		711: [
			0,
			.56597,
			0,
			0,
			.525
		],
		713: [
			0,
			.56555,
			0,
			0,
			.525
		],
		714: [
			0,
			.61111,
			0,
			0,
			.525
		],
		715: [
			0,
			.61111,
			0,
			0,
			.525
		],
		728: [
			0,
			.61111,
			0,
			0,
			.525
		],
		730: [
			0,
			.61111,
			0,
			0,
			.525
		],
		770: [
			0,
			.61111,
			0,
			0,
			.525
		],
		771: [
			0,
			.61111,
			0,
			0,
			.525
		],
		776: [
			0,
			.61111,
			0,
			0,
			.525
		],
		915: [
			0,
			.61111,
			0,
			0,
			.525
		],
		916: [
			0,
			.61111,
			0,
			0,
			.525
		],
		920: [
			0,
			.61111,
			0,
			0,
			.525
		],
		923: [
			0,
			.61111,
			0,
			0,
			.525
		],
		926: [
			0,
			.61111,
			0,
			0,
			.525
		],
		928: [
			0,
			.61111,
			0,
			0,
			.525
		],
		931: [
			0,
			.61111,
			0,
			0,
			.525
		],
		933: [
			0,
			.61111,
			0,
			0,
			.525
		],
		934: [
			0,
			.61111,
			0,
			0,
			.525
		],
		936: [
			0,
			.61111,
			0,
			0,
			.525
		],
		937: [
			0,
			.61111,
			0,
			0,
			.525
		],
		8216: [
			0,
			.61111,
			0,
			0,
			.525
		],
		8217: [
			0,
			.61111,
			0,
			0,
			.525
		],
		8242: [
			0,
			.61111,
			0,
			0,
			.525
		],
		9251: [
			.11111,
			.21944,
			0,
			0,
			.525
		]
	}
}, si = {
	slant: [
		.25,
		.25,
		.25
	],
	space: [
		0,
		0,
		0
	],
	stretch: [
		0,
		0,
		0
	],
	shrink: [
		0,
		0,
		0
	],
	xHeight: [
		.431,
		.431,
		.431
	],
	quad: [
		1,
		1.171,
		1.472
	],
	extraSpace: [
		0,
		0,
		0
	],
	num1: [
		.677,
		.732,
		.925
	],
	num2: [
		.394,
		.384,
		.387
	],
	num3: [
		.444,
		.471,
		.504
	],
	denom1: [
		.686,
		.752,
		1.025
	],
	denom2: [
		.345,
		.344,
		.532
	],
	sup1: [
		.413,
		.503,
		.504
	],
	sup2: [
		.363,
		.431,
		.404
	],
	sup3: [
		.289,
		.286,
		.294
	],
	sub1: [
		.15,
		.143,
		.2
	],
	sub2: [
		.247,
		.286,
		.4
	],
	supDrop: [
		.386,
		.353,
		.494
	],
	subDrop: [
		.05,
		.071,
		.1
	],
	delim1: [
		2.39,
		1.7,
		1.98
	],
	delim2: [
		1.01,
		1.157,
		1.42
	],
	axisHeight: [
		.25,
		.25,
		.25
	],
	defaultRuleThickness: [
		.04,
		.049,
		.049
	],
	bigOpSpacing1: [
		.111,
		.111,
		.111
	],
	bigOpSpacing2: [
		.166,
		.166,
		.166
	],
	bigOpSpacing3: [
		.2,
		.2,
		.2
	],
	bigOpSpacing4: [
		.6,
		.611,
		.611
	],
	bigOpSpacing5: [
		.1,
		.143,
		.143
	],
	sqrtRuleThickness: [
		.04,
		.04,
		.04
	],
	ptPerEm: [
		10,
		10,
		10
	],
	doubleRuleSep: [
		.2,
		.2,
		.2
	],
	arrayRuleWidth: [
		.04,
		.04,
		.04
	],
	fboxsep: [
		.3,
		.3,
		.3
	],
	fboxrule: [
		.04,
		.04,
		.04
	]
}, ci = {
	Å: "A",
	Ð: "D",
	Þ: "o",
	å: "a",
	ð: "d",
	þ: "o",
	А: "A",
	Б: "B",
	В: "B",
	Г: "F",
	Д: "A",
	Е: "E",
	Ж: "K",
	З: "3",
	И: "N",
	Й: "N",
	К: "K",
	Л: "N",
	М: "M",
	Н: "H",
	О: "O",
	П: "N",
	Р: "P",
	С: "C",
	Т: "T",
	У: "y",
	Ф: "O",
	Х: "X",
	Ц: "U",
	Ч: "h",
	Ш: "W",
	Щ: "W",
	Ъ: "B",
	Ы: "X",
	Ь: "B",
	Э: "3",
	Ю: "X",
	Я: "R",
	а: "a",
	б: "b",
	в: "a",
	г: "r",
	д: "y",
	е: "e",
	ж: "m",
	з: "e",
	и: "n",
	й: "n",
	к: "n",
	л: "n",
	м: "m",
	н: "n",
	о: "o",
	п: "n",
	р: "p",
	с: "c",
	т: "o",
	у: "y",
	ф: "b",
	х: "x",
	ц: "n",
	ч: "n",
	ш: "w",
	щ: "w",
	ъ: "a",
	ы: "m",
	ь: "a",
	э: "e",
	ю: "m",
	я: "r"
};
function li(e, t) {
	oi[e] = t;
}
function ui(e, t, n) {
	if (!oi[t]) throw Error("Font metrics not found for font: " + t + ".");
	var r = e.charCodeAt(0), i = oi[t][r];
	if (!i && e[0] in ci && (r = ci[e[0]].charCodeAt(0), i = oi[t][r]), !i && n === "text" && Cr(r) && (i = oi[t][77]), i) return {
		depth: i[0],
		height: i[1],
		italic: i[2],
		skew: i[3],
		width: i[4]
	};
}
var di = {};
function fi(e) {
	var t = e >= 5 ? 0 : e >= 3 ? 1 : 2;
	if (!di[t]) {
		var n = di[t] = { cssEmPerMu: si.quad[t] / 18 };
		for (var r in si) si.hasOwnProperty(r) && (n[r] = si[r][t]);
	}
	return di[t];
}
var pi = {
	math: {},
	text: {}
};
function I(e, t, n, r, i, a) {
	pi[e][i] = {
		font: t,
		group: n,
		replace: r
	}, a && r && (pi[e][r] = pi[e][i]);
}
var L = "math", R = "text", z = "main", B = "ams", mi = "accent-token", V = "bin", hi = "close", gi = "inner", H = "mathord", _i = "op-token", vi = "open", yi = "punct", U = "rel", bi = "spacing", W = "textord";
I(L, z, U, "≡", "\\equiv", !0), I(L, z, U, "≺", "\\prec", !0), I(L, z, U, "≻", "\\succ", !0), I(L, z, U, "∼", "\\sim", !0), I(L, z, U, "⊥", "\\perp"), I(L, z, U, "⪯", "\\preceq", !0), I(L, z, U, "⪰", "\\succeq", !0), I(L, z, U, "≃", "\\simeq", !0), I(L, z, U, "∣", "\\mid", !0), I(L, z, U, "≪", "\\ll", !0), I(L, z, U, "≫", "\\gg", !0), I(L, z, U, "≍", "\\asymp", !0), I(L, z, U, "∥", "\\parallel"), I(L, z, U, "⋈", "\\bowtie", !0), I(L, z, U, "⌣", "\\smile", !0), I(L, z, U, "⊑", "\\sqsubseteq", !0), I(L, z, U, "⊒", "\\sqsupseteq", !0), I(L, z, U, "≐", "\\doteq", !0), I(L, z, U, "⌢", "\\frown", !0), I(L, z, U, "∋", "\\ni", !0), I(L, z, U, "∝", "\\propto", !0), I(L, z, U, "⊢", "\\vdash", !0), I(L, z, U, "⊣", "\\dashv", !0), I(L, z, U, "∋", "\\owns"), I(L, z, yi, ".", "\\ldotp"), I(L, z, yi, "⋅", "\\cdotp"), I(L, z, yi, "⋅", "·"), I(R, z, W, "⋅", "·"), I(L, z, W, "#", "\\#"), I(R, z, W, "#", "\\#"), I(L, z, W, "&", "\\&"), I(R, z, W, "&", "\\&"), I(L, z, W, "ℵ", "\\aleph", !0), I(L, z, W, "∀", "\\forall", !0), I(L, z, W, "ℏ", "\\hbar", !0), I(L, z, W, "∃", "\\exists", !0), I(L, z, W, "∇", "\\nabla", !0), I(L, z, W, "♭", "\\flat", !0), I(L, z, W, "ℓ", "\\ell", !0), I(L, z, W, "♮", "\\natural", !0), I(L, z, W, "♣", "\\clubsuit", !0), I(L, z, W, "℘", "\\wp", !0), I(L, z, W, "♯", "\\sharp", !0), I(L, z, W, "♢", "\\diamondsuit", !0), I(L, z, W, "ℜ", "\\Re", !0), I(L, z, W, "♡", "\\heartsuit", !0), I(L, z, W, "ℑ", "\\Im", !0), I(L, z, W, "♠", "\\spadesuit", !0), I(L, z, W, "§", "\\S", !0), I(R, z, W, "§", "\\S"), I(L, z, W, "¶", "\\P", !0), I(R, z, W, "¶", "\\P"), I(L, z, W, "†", "\\dag"), I(R, z, W, "†", "\\dag"), I(R, z, W, "†", "\\textdagger"), I(L, z, W, "‡", "\\ddag"), I(R, z, W, "‡", "\\ddag"), I(R, z, W, "‡", "\\textdaggerdbl"), I(L, z, hi, "⎱", "\\rmoustache", !0), I(L, z, vi, "⎰", "\\lmoustache", !0), I(L, z, hi, "⟯", "\\rgroup", !0), I(L, z, vi, "⟮", "\\lgroup", !0), I(L, z, V, "∓", "\\mp", !0), I(L, z, V, "⊖", "\\ominus", !0), I(L, z, V, "⊎", "\\uplus", !0), I(L, z, V, "⊓", "\\sqcap", !0), I(L, z, V, "∗", "\\ast"), I(L, z, V, "⊔", "\\sqcup", !0), I(L, z, V, "◯", "\\bigcirc", !0), I(L, z, V, "∙", "\\bullet", !0), I(L, z, V, "‡", "\\ddagger"), I(L, z, V, "≀", "\\wr", !0), I(L, z, V, "⨿", "\\amalg"), I(L, z, V, "&", "\\And"), I(L, z, U, "⟵", "\\longleftarrow", !0), I(L, z, U, "⇐", "\\Leftarrow", !0), I(L, z, U, "⟸", "\\Longleftarrow", !0), I(L, z, U, "⟶", "\\longrightarrow", !0), I(L, z, U, "⇒", "\\Rightarrow", !0), I(L, z, U, "⟹", "\\Longrightarrow", !0), I(L, z, U, "↔", "\\leftrightarrow", !0), I(L, z, U, "⟷", "\\longleftrightarrow", !0), I(L, z, U, "⇔", "\\Leftrightarrow", !0), I(L, z, U, "⟺", "\\Longleftrightarrow", !0), I(L, z, U, "↦", "\\mapsto", !0), I(L, z, U, "⟼", "\\longmapsto", !0), I(L, z, U, "↗", "\\nearrow", !0), I(L, z, U, "↩", "\\hookleftarrow", !0), I(L, z, U, "↪", "\\hookrightarrow", !0), I(L, z, U, "↘", "\\searrow", !0), I(L, z, U, "↼", "\\leftharpoonup", !0), I(L, z, U, "⇀", "\\rightharpoonup", !0), I(L, z, U, "↙", "\\swarrow", !0), I(L, z, U, "↽", "\\leftharpoondown", !0), I(L, z, U, "⇁", "\\rightharpoondown", !0), I(L, z, U, "↖", "\\nwarrow", !0), I(L, z, U, "⇌", "\\rightleftharpoons", !0), I(L, B, U, "≮", "\\nless", !0), I(L, B, U, "", "\\@nleqslant"), I(L, B, U, "", "\\@nleqq"), I(L, B, U, "⪇", "\\lneq", !0), I(L, B, U, "≨", "\\lneqq", !0), I(L, B, U, "", "\\@lvertneqq"), I(L, B, U, "⋦", "\\lnsim", !0), I(L, B, U, "⪉", "\\lnapprox", !0), I(L, B, U, "⊀", "\\nprec", !0), I(L, B, U, "⋠", "\\npreceq", !0), I(L, B, U, "⋨", "\\precnsim", !0), I(L, B, U, "⪹", "\\precnapprox", !0), I(L, B, U, "≁", "\\nsim", !0), I(L, B, U, "", "\\@nshortmid"), I(L, B, U, "∤", "\\nmid", !0), I(L, B, U, "⊬", "\\nvdash", !0), I(L, B, U, "⊭", "\\nvDash", !0), I(L, B, U, "⋪", "\\ntriangleleft"), I(L, B, U, "⋬", "\\ntrianglelefteq", !0), I(L, B, U, "⊊", "\\subsetneq", !0), I(L, B, U, "", "\\@varsubsetneq"), I(L, B, U, "⫋", "\\subsetneqq", !0), I(L, B, U, "", "\\@varsubsetneqq"), I(L, B, U, "≯", "\\ngtr", !0), I(L, B, U, "", "\\@ngeqslant"), I(L, B, U, "", "\\@ngeqq"), I(L, B, U, "⪈", "\\gneq", !0), I(L, B, U, "≩", "\\gneqq", !0), I(L, B, U, "", "\\@gvertneqq"), I(L, B, U, "⋧", "\\gnsim", !0), I(L, B, U, "⪊", "\\gnapprox", !0), I(L, B, U, "⊁", "\\nsucc", !0), I(L, B, U, "⋡", "\\nsucceq", !0), I(L, B, U, "⋩", "\\succnsim", !0), I(L, B, U, "⪺", "\\succnapprox", !0), I(L, B, U, "≆", "\\ncong", !0), I(L, B, U, "", "\\@nshortparallel"), I(L, B, U, "∦", "\\nparallel", !0), I(L, B, U, "⊯", "\\nVDash", !0), I(L, B, U, "⋫", "\\ntriangleright"), I(L, B, U, "⋭", "\\ntrianglerighteq", !0), I(L, B, U, "", "\\@nsupseteqq"), I(L, B, U, "⊋", "\\supsetneq", !0), I(L, B, U, "", "\\@varsupsetneq"), I(L, B, U, "⫌", "\\supsetneqq", !0), I(L, B, U, "", "\\@varsupsetneqq"), I(L, B, U, "⊮", "\\nVdash", !0), I(L, B, U, "⪵", "\\precneqq", !0), I(L, B, U, "⪶", "\\succneqq", !0), I(L, B, U, "", "\\@nsubseteqq"), I(L, B, V, "⊴", "\\unlhd"), I(L, B, V, "⊵", "\\unrhd"), I(L, B, U, "↚", "\\nleftarrow", !0), I(L, B, U, "↛", "\\nrightarrow", !0), I(L, B, U, "⇍", "\\nLeftarrow", !0), I(L, B, U, "⇏", "\\nRightarrow", !0), I(L, B, U, "↮", "\\nleftrightarrow", !0), I(L, B, U, "⇎", "\\nLeftrightarrow", !0), I(L, B, U, "△", "\\vartriangle"), I(L, B, W, "ℏ", "\\hslash"), I(L, B, W, "▽", "\\triangledown"), I(L, B, W, "◊", "\\lozenge"), I(L, B, W, "Ⓢ", "\\circledS"), I(L, B, W, "®", "\\circledR"), I(R, B, W, "®", "\\circledR"), I(L, B, W, "∡", "\\measuredangle", !0), I(L, B, W, "∄", "\\nexists"), I(L, B, W, "℧", "\\mho"), I(L, B, W, "Ⅎ", "\\Finv", !0), I(L, B, W, "⅁", "\\Game", !0), I(L, B, W, "‵", "\\backprime"), I(L, B, W, "▲", "\\blacktriangle"), I(L, B, W, "▼", "\\blacktriangledown"), I(L, B, W, "■", "\\blacksquare"), I(L, B, W, "⧫", "\\blacklozenge"), I(L, B, W, "★", "\\bigstar"), I(L, B, W, "∢", "\\sphericalangle", !0), I(L, B, W, "∁", "\\complement", !0), I(L, B, W, "ð", "\\eth", !0), I(R, z, W, "ð", "ð"), I(L, B, W, "╱", "\\diagup"), I(L, B, W, "╲", "\\diagdown"), I(L, B, W, "□", "\\square"), I(L, B, W, "□", "\\Box"), I(L, B, W, "◊", "\\Diamond"), I(L, B, W, "¥", "\\yen", !0), I(R, B, W, "¥", "\\yen", !0), I(L, B, W, "✓", "\\checkmark", !0), I(R, B, W, "✓", "\\checkmark"), I(L, B, W, "ℶ", "\\beth", !0), I(L, B, W, "ℸ", "\\daleth", !0), I(L, B, W, "ℷ", "\\gimel", !0), I(L, B, W, "ϝ", "\\digamma", !0), I(L, B, W, "ϰ", "\\varkappa"), I(L, B, vi, "┌", "\\@ulcorner", !0), I(L, B, hi, "┐", "\\@urcorner", !0), I(L, B, vi, "└", "\\@llcorner", !0), I(L, B, hi, "┘", "\\@lrcorner", !0), I(L, B, U, "≦", "\\leqq", !0), I(L, B, U, "⩽", "\\leqslant", !0), I(L, B, U, "⪕", "\\eqslantless", !0), I(L, B, U, "≲", "\\lesssim", !0), I(L, B, U, "⪅", "\\lessapprox", !0), I(L, B, U, "≊", "\\approxeq", !0), I(L, B, V, "⋖", "\\lessdot"), I(L, B, U, "⋘", "\\lll", !0), I(L, B, U, "≶", "\\lessgtr", !0), I(L, B, U, "⋚", "\\lesseqgtr", !0), I(L, B, U, "⪋", "\\lesseqqgtr", !0), I(L, B, U, "≑", "\\doteqdot"), I(L, B, U, "≓", "\\risingdotseq", !0), I(L, B, U, "≒", "\\fallingdotseq", !0), I(L, B, U, "∽", "\\backsim", !0), I(L, B, U, "⋍", "\\backsimeq", !0), I(L, B, U, "⫅", "\\subseteqq", !0), I(L, B, U, "⋐", "\\Subset", !0), I(L, B, U, "⊏", "\\sqsubset", !0), I(L, B, U, "≼", "\\preccurlyeq", !0), I(L, B, U, "⋞", "\\curlyeqprec", !0), I(L, B, U, "≾", "\\precsim", !0), I(L, B, U, "⪷", "\\precapprox", !0), I(L, B, U, "⊲", "\\vartriangleleft"), I(L, B, U, "⊴", "\\trianglelefteq"), I(L, B, U, "⊨", "\\vDash", !0), I(L, B, U, "⊪", "\\Vvdash", !0), I(L, B, U, "⌣", "\\smallsmile"), I(L, B, U, "⌢", "\\smallfrown"), I(L, B, U, "≏", "\\bumpeq", !0), I(L, B, U, "≎", "\\Bumpeq", !0), I(L, B, U, "≧", "\\geqq", !0), I(L, B, U, "⩾", "\\geqslant", !0), I(L, B, U, "⪖", "\\eqslantgtr", !0), I(L, B, U, "≳", "\\gtrsim", !0), I(L, B, U, "⪆", "\\gtrapprox", !0), I(L, B, V, "⋗", "\\gtrdot"), I(L, B, U, "⋙", "\\ggg", !0), I(L, B, U, "≷", "\\gtrless", !0), I(L, B, U, "⋛", "\\gtreqless", !0), I(L, B, U, "⪌", "\\gtreqqless", !0), I(L, B, U, "≖", "\\eqcirc", !0), I(L, B, U, "≗", "\\circeq", !0), I(L, B, U, "≜", "\\triangleq", !0), I(L, B, U, "∼", "\\thicksim"), I(L, B, U, "≈", "\\thickapprox"), I(L, B, U, "⫆", "\\supseteqq", !0), I(L, B, U, "⋑", "\\Supset", !0), I(L, B, U, "⊐", "\\sqsupset", !0), I(L, B, U, "≽", "\\succcurlyeq", !0), I(L, B, U, "⋟", "\\curlyeqsucc", !0), I(L, B, U, "≿", "\\succsim", !0), I(L, B, U, "⪸", "\\succapprox", !0), I(L, B, U, "⊳", "\\vartriangleright"), I(L, B, U, "⊵", "\\trianglerighteq"), I(L, B, U, "⊩", "\\Vdash", !0), I(L, B, U, "∣", "\\shortmid"), I(L, B, U, "∥", "\\shortparallel"), I(L, B, U, "≬", "\\between", !0), I(L, B, U, "⋔", "\\pitchfork", !0), I(L, B, U, "∝", "\\varpropto"), I(L, B, U, "◀", "\\blacktriangleleft"), I(L, B, U, "∴", "\\therefore", !0), I(L, B, U, "∍", "\\backepsilon"), I(L, B, U, "▶", "\\blacktriangleright"), I(L, B, U, "∵", "\\because", !0), I(L, B, U, "⋘", "\\llless"), I(L, B, U, "⋙", "\\gggtr"), I(L, B, V, "⊲", "\\lhd"), I(L, B, V, "⊳", "\\rhd"), I(L, B, U, "≂", "\\eqsim", !0), I(L, z, U, "⋈", "\\Join"), I(L, B, U, "≑", "\\Doteq", !0), I(L, B, V, "∔", "\\dotplus", !0), I(L, B, V, "∖", "\\smallsetminus"), I(L, B, V, "⋒", "\\Cap", !0), I(L, B, V, "⋓", "\\Cup", !0), I(L, B, V, "⩞", "\\doublebarwedge", !0), I(L, B, V, "⊟", "\\boxminus", !0), I(L, B, V, "⊞", "\\boxplus", !0), I(L, B, V, "⋇", "\\divideontimes", !0), I(L, B, V, "⋉", "\\ltimes", !0), I(L, B, V, "⋊", "\\rtimes", !0), I(L, B, V, "⋋", "\\leftthreetimes", !0), I(L, B, V, "⋌", "\\rightthreetimes", !0), I(L, B, V, "⋏", "\\curlywedge", !0), I(L, B, V, "⋎", "\\curlyvee", !0), I(L, B, V, "⊝", "\\circleddash", !0), I(L, B, V, "⊛", "\\circledast", !0), I(L, B, V, "⋅", "\\centerdot"), I(L, B, V, "⊺", "\\intercal", !0), I(L, B, V, "⋒", "\\doublecap"), I(L, B, V, "⋓", "\\doublecup"), I(L, B, V, "⊠", "\\boxtimes", !0), I(L, B, U, "⇢", "\\dashrightarrow", !0), I(L, B, U, "⇠", "\\dashleftarrow", !0), I(L, B, U, "⇇", "\\leftleftarrows", !0), I(L, B, U, "⇆", "\\leftrightarrows", !0), I(L, B, U, "⇚", "\\Lleftarrow", !0), I(L, B, U, "↞", "\\twoheadleftarrow", !0), I(L, B, U, "↢", "\\leftarrowtail", !0), I(L, B, U, "↫", "\\looparrowleft", !0), I(L, B, U, "⇋", "\\leftrightharpoons", !0), I(L, B, U, "↶", "\\curvearrowleft", !0), I(L, B, U, "↺", "\\circlearrowleft", !0), I(L, B, U, "↰", "\\Lsh", !0), I(L, B, U, "⇈", "\\upuparrows", !0), I(L, B, U, "↿", "\\upharpoonleft", !0), I(L, B, U, "⇃", "\\downharpoonleft", !0), I(L, z, U, "⊶", "\\origof", !0), I(L, z, U, "⊷", "\\imageof", !0), I(L, B, U, "⊸", "\\multimap", !0), I(L, B, U, "↭", "\\leftrightsquigarrow", !0), I(L, B, U, "⇉", "\\rightrightarrows", !0), I(L, B, U, "⇄", "\\rightleftarrows", !0), I(L, B, U, "↠", "\\twoheadrightarrow", !0), I(L, B, U, "↣", "\\rightarrowtail", !0), I(L, B, U, "↬", "\\looparrowright", !0), I(L, B, U, "↷", "\\curvearrowright", !0), I(L, B, U, "↻", "\\circlearrowright", !0), I(L, B, U, "↱", "\\Rsh", !0), I(L, B, U, "⇊", "\\downdownarrows", !0), I(L, B, U, "↾", "\\upharpoonright", !0), I(L, B, U, "⇂", "\\downharpoonright", !0), I(L, B, U, "⇝", "\\rightsquigarrow", !0), I(L, B, U, "⇝", "\\leadsto"), I(L, B, U, "⇛", "\\Rrightarrow", !0), I(L, B, U, "↾", "\\restriction"), I(L, z, W, "‘", "`"), I(L, z, W, "$", "\\$"), I(R, z, W, "$", "\\$"), I(R, z, W, "$", "\\textdollar"), I(L, z, W, "%", "\\%"), I(R, z, W, "%", "\\%"), I(L, z, W, "_", "\\_"), I(R, z, W, "_", "\\_"), I(R, z, W, "_", "\\textunderscore"), I(L, z, W, "∠", "\\angle", !0), I(L, z, W, "∞", "\\infty", !0), I(L, z, W, "′", "\\prime"), I(L, z, W, "△", "\\triangle"), I(L, z, W, "Γ", "\\Gamma", !0), I(L, z, W, "Δ", "\\Delta", !0), I(L, z, W, "Θ", "\\Theta", !0), I(L, z, W, "Λ", "\\Lambda", !0), I(L, z, W, "Ξ", "\\Xi", !0), I(L, z, W, "Π", "\\Pi", !0), I(L, z, W, "Σ", "\\Sigma", !0), I(L, z, W, "Υ", "\\Upsilon", !0), I(L, z, W, "Φ", "\\Phi", !0), I(L, z, W, "Ψ", "\\Psi", !0), I(L, z, W, "Ω", "\\Omega", !0), I(L, z, W, "A", "Α"), I(L, z, W, "B", "Β"), I(L, z, W, "E", "Ε"), I(L, z, W, "Z", "Ζ"), I(L, z, W, "H", "Η"), I(L, z, W, "I", "Ι"), I(L, z, W, "K", "Κ"), I(L, z, W, "M", "Μ"), I(L, z, W, "N", "Ν"), I(L, z, W, "O", "Ο"), I(L, z, W, "P", "Ρ"), I(L, z, W, "T", "Τ"), I(L, z, W, "X", "Χ"), I(L, z, W, "¬", "\\neg", !0), I(L, z, W, "¬", "\\lnot"), I(L, z, W, "⊤", "\\top"), I(L, z, W, "⊥", "\\bot"), I(L, z, W, "∅", "\\emptyset"), I(L, B, W, "∅", "\\varnothing"), I(L, z, H, "α", "\\alpha", !0), I(L, z, H, "β", "\\beta", !0), I(L, z, H, "γ", "\\gamma", !0), I(L, z, H, "δ", "\\delta", !0), I(L, z, H, "ϵ", "\\epsilon", !0), I(L, z, H, "ζ", "\\zeta", !0), I(L, z, H, "η", "\\eta", !0), I(L, z, H, "θ", "\\theta", !0), I(L, z, H, "ι", "\\iota", !0), I(L, z, H, "κ", "\\kappa", !0), I(L, z, H, "λ", "\\lambda", !0), I(L, z, H, "μ", "\\mu", !0), I(L, z, H, "ν", "\\nu", !0), I(L, z, H, "ξ", "\\xi", !0), I(L, z, H, "ο", "\\omicron", !0), I(L, z, H, "π", "\\pi", !0), I(L, z, H, "ρ", "\\rho", !0), I(L, z, H, "σ", "\\sigma", !0), I(L, z, H, "τ", "\\tau", !0), I(L, z, H, "υ", "\\upsilon", !0), I(L, z, H, "ϕ", "\\phi", !0), I(L, z, H, "χ", "\\chi", !0), I(L, z, H, "ψ", "\\psi", !0), I(L, z, H, "ω", "\\omega", !0), I(L, z, H, "ε", "\\varepsilon", !0), I(L, z, H, "ϑ", "\\vartheta", !0), I(L, z, H, "ϖ", "\\varpi", !0), I(L, z, H, "ϱ", "\\varrho", !0), I(L, z, H, "ς", "\\varsigma", !0), I(L, z, H, "φ", "\\varphi", !0), I(L, z, V, "∗", "*", !0), I(L, z, V, "+", "+"), I(L, z, V, "−", "-", !0), I(L, z, V, "⋅", "\\cdot", !0), I(L, z, V, "∘", "\\circ", !0), I(L, z, V, "÷", "\\div", !0), I(L, z, V, "±", "\\pm", !0), I(L, z, V, "×", "\\times", !0), I(L, z, V, "∩", "\\cap", !0), I(L, z, V, "∪", "\\cup", !0), I(L, z, V, "∖", "\\setminus", !0), I(L, z, V, "∧", "\\land"), I(L, z, V, "∨", "\\lor"), I(L, z, V, "∧", "\\wedge", !0), I(L, z, V, "∨", "\\vee", !0), I(L, z, W, "√", "\\surd"), I(L, z, vi, "⟨", "\\langle", !0), I(L, z, vi, "∣", "\\lvert"), I(L, z, vi, "∥", "\\lVert"), I(L, z, hi, "?", "?"), I(L, z, hi, "!", "!"), I(L, z, hi, "⟩", "\\rangle", !0), I(L, z, hi, "∣", "\\rvert"), I(L, z, hi, "∥", "\\rVert"), I(L, z, U, "=", "="), I(L, z, U, ":", ":"), I(L, z, U, "≈", "\\approx", !0), I(L, z, U, "≅", "\\cong", !0), I(L, z, U, "≥", "\\ge"), I(L, z, U, "≥", "\\geq", !0), I(L, z, U, "←", "\\gets"), I(L, z, U, ">", "\\gt", !0), I(L, z, U, "∈", "\\in", !0), I(L, z, U, "", "\\@not"), I(L, z, U, "⊂", "\\subset", !0), I(L, z, U, "⊃", "\\supset", !0), I(L, z, U, "⊆", "\\subseteq", !0), I(L, z, U, "⊇", "\\supseteq", !0), I(L, B, U, "⊈", "\\nsubseteq", !0), I(L, B, U, "⊉", "\\nsupseteq", !0), I(L, z, U, "⊨", "\\models"), I(L, z, U, "←", "\\leftarrow", !0), I(L, z, U, "≤", "\\le"), I(L, z, U, "≤", "\\leq", !0), I(L, z, U, "<", "\\lt", !0), I(L, z, U, "→", "\\rightarrow", !0), I(L, z, U, "→", "\\to"), I(L, B, U, "≱", "\\ngeq", !0), I(L, B, U, "≰", "\\nleq", !0), I(L, z, bi, "\xA0", "\\ "), I(L, z, bi, "\xA0", "\\space"), I(L, z, bi, "\xA0", "\\nobreakspace"), I(R, z, bi, "\xA0", "\\ "), I(R, z, bi, "\xA0", " "), I(R, z, bi, "\xA0", "\\space"), I(R, z, bi, "\xA0", "\\nobreakspace"), I(L, z, bi, "", "\\nobreak"), I(L, z, bi, "", "\\allowbreak"), I(L, z, yi, ",", ","), I(L, z, yi, ";", ";"), I(L, B, V, "⊼", "\\barwedge", !0), I(L, B, V, "⊻", "\\veebar", !0), I(L, z, V, "⊙", "\\odot", !0), I(L, z, V, "⊕", "\\oplus", !0), I(L, z, V, "⊗", "\\otimes", !0), I(L, z, W, "∂", "\\partial", !0), I(L, z, V, "⊘", "\\oslash", !0), I(L, B, V, "⊚", "\\circledcirc", !0), I(L, B, V, "⊡", "\\boxdot", !0), I(L, z, V, "△", "\\bigtriangleup"), I(L, z, V, "▽", "\\bigtriangledown"), I(L, z, V, "†", "\\dagger"), I(L, z, V, "⋄", "\\diamond"), I(L, z, V, "⋆", "\\star"), I(L, z, V, "◃", "\\triangleleft"), I(L, z, V, "▹", "\\triangleright"), I(L, z, vi, "{", "\\{"), I(R, z, W, "{", "\\{"), I(R, z, W, "{", "\\textbraceleft"), I(L, z, hi, "}", "\\}"), I(R, z, W, "}", "\\}"), I(R, z, W, "}", "\\textbraceright"), I(L, z, vi, "{", "\\lbrace"), I(L, z, hi, "}", "\\rbrace"), I(L, z, vi, "[", "\\lbrack", !0), I(R, z, W, "[", "\\lbrack", !0), I(L, z, hi, "]", "\\rbrack", !0), I(R, z, W, "]", "\\rbrack", !0), I(L, z, vi, "(", "\\lparen", !0), I(L, z, hi, ")", "\\rparen", !0), I(R, z, W, "<", "\\textless", !0), I(R, z, W, ">", "\\textgreater", !0), I(L, z, vi, "⌊", "\\lfloor", !0), I(L, z, hi, "⌋", "\\rfloor", !0), I(L, z, vi, "⌈", "\\lceil", !0), I(L, z, hi, "⌉", "\\rceil", !0), I(L, z, W, "\\", "\\backslash"), I(L, z, W, "∣", "|"), I(L, z, W, "∣", "\\vert"), I(R, z, W, "|", "\\textbar", !0), I(L, z, W, "∥", "\\|"), I(L, z, W, "∥", "\\Vert"), I(R, z, W, "∥", "\\textbardbl"), I(R, z, W, "~", "\\textasciitilde"), I(R, z, W, "\\", "\\textbackslash"), I(R, z, W, "^", "\\textasciicircum"), I(L, z, U, "↑", "\\uparrow", !0), I(L, z, U, "⇑", "\\Uparrow", !0), I(L, z, U, "↓", "\\downarrow", !0), I(L, z, U, "⇓", "\\Downarrow", !0), I(L, z, U, "↕", "\\updownarrow", !0), I(L, z, U, "⇕", "\\Updownarrow", !0), I(L, z, _i, "∐", "\\coprod"), I(L, z, _i, "⋁", "\\bigvee"), I(L, z, _i, "⋀", "\\bigwedge"), I(L, z, _i, "⨄", "\\biguplus"), I(L, z, _i, "⋂", "\\bigcap"), I(L, z, _i, "⋃", "\\bigcup"), I(L, z, _i, "∫", "\\int"), I(L, z, _i, "∫", "\\intop"), I(L, z, _i, "∬", "\\iint"), I(L, z, _i, "∭", "\\iiint"), I(L, z, _i, "∏", "\\prod"), I(L, z, _i, "∑", "\\sum"), I(L, z, _i, "⨂", "\\bigotimes"), I(L, z, _i, "⨁", "\\bigoplus"), I(L, z, _i, "⨀", "\\bigodot"), I(L, z, _i, "∮", "\\oint"), I(L, z, _i, "∯", "\\oiint"), I(L, z, _i, "∰", "\\oiiint"), I(L, z, _i, "⨆", "\\bigsqcup"), I(L, z, _i, "∫", "\\smallint"), I(R, z, gi, "…", "\\textellipsis"), I(L, z, gi, "…", "\\mathellipsis"), I(R, z, gi, "…", "\\ldots", !0), I(L, z, gi, "…", "\\ldots", !0), I(L, z, gi, "⋯", "\\@cdots", !0), I(L, z, gi, "⋱", "\\ddots", !0), I(L, z, W, "⋮", "\\varvdots"), I(R, z, W, "⋮", "\\varvdots"), I(L, z, mi, "ˊ", "\\acute"), I(L, z, mi, "ˋ", "\\grave"), I(L, z, mi, "¨", "\\ddot"), I(L, z, mi, "~", "\\tilde"), I(L, z, mi, "ˉ", "\\bar"), I(L, z, mi, "˘", "\\breve"), I(L, z, mi, "ˇ", "\\check"), I(L, z, mi, "^", "\\hat"), I(L, z, mi, "⃗", "\\vec"), I(L, z, mi, "˙", "\\dot"), I(L, z, mi, "˚", "\\mathring"), I(L, z, H, "", "\\@imath"), I(L, z, H, "", "\\@jmath"), I(L, z, W, "ı", "ı"), I(L, z, W, "ȷ", "ȷ"), I(R, z, W, "ı", "\\i", !0), I(R, z, W, "ȷ", "\\j", !0), I(R, z, W, "ß", "\\ss", !0), I(R, z, W, "æ", "\\ae", !0), I(R, z, W, "œ", "\\oe", !0), I(R, z, W, "ø", "\\o", !0), I(R, z, W, "Æ", "\\AE", !0), I(R, z, W, "Œ", "\\OE", !0), I(R, z, W, "Ø", "\\O", !0), I(R, z, mi, "ˊ", "\\'"), I(R, z, mi, "ˋ", "\\`"), I(R, z, mi, "ˆ", "\\^"), I(R, z, mi, "˜", "\\~"), I(R, z, mi, "ˉ", "\\="), I(R, z, mi, "˘", "\\u"), I(R, z, mi, "˙", "\\."), I(R, z, mi, "¸", "\\c"), I(R, z, mi, "˚", "\\r"), I(R, z, mi, "ˇ", "\\v"), I(R, z, mi, "¨", "\\\""), I(R, z, mi, "˝", "\\H"), I(R, z, mi, "◯", "\\textcircled");
var xi = {
	"--": !0,
	"---": !0,
	"``": !0,
	"''": !0
};
I(R, z, W, "–", "--", !0), I(R, z, W, "–", "\\textendash"), I(R, z, W, "—", "---", !0), I(R, z, W, "—", "\\textemdash"), I(R, z, W, "‘", "`", !0), I(R, z, W, "‘", "\\textquoteleft"), I(R, z, W, "’", "'", !0), I(R, z, W, "’", "\\textquoteright"), I(R, z, W, "“", "``", !0), I(R, z, W, "“", "\\textquotedblleft"), I(R, z, W, "”", "''", !0), I(R, z, W, "”", "\\textquotedblright"), I(L, z, W, "°", "\\degree", !0), I(R, z, W, "°", "\\degree"), I(R, z, W, "°", "\\textdegree", !0), I(L, z, W, "£", "\\pounds"), I(L, z, W, "£", "\\mathsterling", !0), I(R, z, W, "£", "\\pounds"), I(R, z, W, "£", "\\textsterling", !0), I(L, B, W, "✠", "\\maltese"), I(R, B, W, "✠", "\\maltese");
for (var Si = "0123456789/@.\"", Ci = 0; Ci < Si.length; Ci++) {
	var wi = Si.charAt(Ci);
	I(L, z, W, wi, wi);
}
for (var Ti = "0123456789!@*()-=+\";:?/.,", Ei = 0; Ei < Ti.length; Ei++) {
	var Di = Ti.charAt(Ei);
	I(R, z, W, Di, Di);
}
for (var Oi = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", ki = 0; ki < Oi.length; ki++) {
	var Ai = Oi.charAt(ki);
	I(L, z, H, Ai, Ai), I(R, z, W, Ai, Ai);
}
I(L, B, W, "C", "ℂ"), I(R, B, W, "C", "ℂ"), I(L, B, W, "H", "ℍ"), I(R, B, W, "H", "ℍ"), I(L, B, W, "N", "ℕ"), I(R, B, W, "N", "ℕ"), I(L, B, W, "P", "ℙ"), I(R, B, W, "P", "ℙ"), I(L, B, W, "Q", "ℚ"), I(R, B, W, "Q", "ℚ"), I(L, B, W, "R", "ℝ"), I(R, B, W, "R", "ℝ"), I(L, B, W, "Z", "ℤ"), I(R, B, W, "Z", "ℤ"), I(L, z, H, "h", "ℎ"), I(R, z, H, "h", "ℎ");
for (var G, ji = 0; ji < Oi.length; ji++) {
	var Mi = Oi.charAt(ji);
	G = String.fromCharCode(55349, 56320 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56372 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56424 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56580 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56684 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56736 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56788 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56840 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56944 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), ji < 26 && (G = String.fromCharCode(55349, 56632 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G), G = String.fromCharCode(55349, 56476 + ji), I(L, z, H, Mi, G), I(R, z, W, Mi, G));
}
G = String.fromCharCode(55349, 56668), I(L, z, H, "k", G), I(R, z, W, "k", G);
for (var Ni = 0; Ni < 10; Ni++) {
	var Pi = Ni.toString();
	G = String.fromCharCode(55349, 57294 + Ni), I(L, z, H, Pi, G), I(R, z, W, Pi, G), G = String.fromCharCode(55349, 57314 + Ni), I(L, z, H, Pi, G), I(R, z, W, Pi, G), G = String.fromCharCode(55349, 57324 + Ni), I(L, z, H, Pi, G), I(R, z, W, Pi, G), G = String.fromCharCode(55349, 57334 + Ni), I(L, z, H, Pi, G), I(R, z, W, Pi, G);
}
for (var Fi = "ÐÞþ", Ii = 0; Ii < Fi.length; Ii++) {
	var Li = Fi.charAt(Ii);
	I(L, z, H, Li, Li), I(R, z, W, Li, Li);
}
var Ri = {
	mathClass: "mathbf",
	textClass: "textbf",
	font: "Main-Bold"
}, zi = {
	mathClass: "mathnormal",
	textClass: "textit",
	font: "Math-Italic"
}, Bi = {
	mathClass: "boldsymbol",
	textClass: "boldsymbol",
	font: "Main-BoldItalic"
}, Vi = {
	mathClass: "mathscr",
	textClass: "textscr",
	font: "Script-Regular"
}, Hi = {
	mathClass: "",
	textClass: "",
	font: ""
}, Ui = {
	mathClass: "mathfrak",
	textClass: "textfrak",
	font: "Fraktur-Regular"
}, Wi = {
	mathClass: "mathbb",
	textClass: "textbb",
	font: "AMS-Regular"
}, Gi = {
	mathClass: "mathboldfrak",
	textClass: "textboldfrak",
	font: "Fraktur-Regular"
}, Ki = {
	mathClass: "mathsf",
	textClass: "textsf",
	font: "SansSerif-Regular"
}, qi = {
	mathClass: "mathboldsf",
	textClass: "textboldsf",
	font: "SansSerif-Bold"
}, Ji = {
	mathClass: "mathitsf",
	textClass: "textitsf",
	font: "SansSerif-Italic"
}, Yi = {
	mathClass: "mathtt",
	textClass: "texttt",
	font: "Typewriter-Regular"
}, Xi = [
	Ri,
	Ri,
	zi,
	zi,
	Bi,
	Bi,
	Vi,
	Hi,
	Hi,
	Hi,
	Ui,
	Ui,
	Wi,
	Wi,
	Gi,
	Gi,
	Ki,
	Ki,
	qi,
	qi,
	Ji,
	Ji,
	Hi,
	Hi,
	Yi,
	Yi
], Zi = [
	Ri,
	Hi,
	Ki,
	qi,
	Yi
], Qi = (e) => {
	var t = e.charCodeAt(0), n = e.charCodeAt(1), r = (t - 55296) * 1024 + (n - 56320) + 65536;
	if (119808 <= r && r < 120484) return Xi[Math.floor((r - 119808) / 26)];
	if (120782 <= r && r <= 120831) return Zi[Math.floor((r - 120782) / 10)];
	if (r === 120485 || r === 120486) return Xi[0];
	if (120486 < r && r < 120782) return Hi;
	throw new N("Unsupported character: " + e);
}, $i = function(e, t, n) {
	if (pi[n][e]) {
		var r = pi[n][e].replace;
		r && (e = r);
	}
	return {
		value: e,
		metrics: ui(e, t, n)
	};
}, ea = function(e, t, n, r, i) {
	var a = $i(e, t, n), o = a.metrics;
	e = a.value;
	var s;
	if (o) {
		var c = o.italic;
		(n === "text" || r && r.font === "mathit") && (c = 0), s = new $r(e, o.height, o.depth, c, o.skew, o.width, i);
	} else typeof console < "u" && console.warn("No character metrics " + ("for '" + e + "' in style '" + t + "' and mode '" + n + "'")), s = new $r(e, 0, 0, 0, 0, 0, i);
	if (r) {
		s.maxFontSize = r.sizeMultiplier, r.style.isTight() && s.classes.push("mtight");
		var l = r.getColor();
		l && (s.style.color = l);
	}
	return s;
}, ta = function(e, t, n, r) {
	return r === void 0 && (r = []), n.font === "boldsymbol" && $i(e, "Main-Bold", t).metrics ? ea(e, "Main-Bold", t, n, r.concat(["mathbf"])) : e === "\\" || pi[t][e].font === "main" ? ea(e, "Main-Regular", t, n, r) : ea(e, "AMS-Regular", t, n, r.concat(["amsrm"]));
}, na = function(e, t, n) {
	return n !== "textord" && $i(e, "Math-BoldItalic", t).metrics ? {
		fontName: "Math-BoldItalic",
		fontClass: "boldsymbol"
	} : {
		fontName: "Main-Bold",
		fontClass: "mathbf"
	};
}, ra = function(e, t, n) {
	var r = e.mode, i = e.text, a = ["mord"], { font: o, fontFamily: s, fontWeight: c, fontShape: l } = t, u = r === "math" || r === "text" && !!o, d = u ? o : s, f = "", p = "";
	if (i.charCodeAt(0) === 55349) {
		var m = Qi(i);
		f = m.font, p = m[r + "Class"];
	}
	if (f) return ea(i, f, r, t, a.concat(p));
	if (d) {
		var h, g;
		if (d === "boldsymbol") {
			var _ = na(i, r, n);
			h = _.fontName, g = [_.fontClass];
		} else u ? (h = ga[o].fontName, g = [o]) : (h = ha(s, c, l), g = [
			s,
			c,
			l
		]);
		if ($i(i, h, r).metrics) return ea(i, h, r, t, a.concat(g));
		if (xi.hasOwnProperty(i) && h.slice(0, 10) === "Typewriter") {
			for (var v = [], y = 0; y < i.length; y++) v.push(ea(i[y], h, r, t, a.concat(g)));
			return ua(v);
		}
	}
	if (n === "mathord") return ea(i, "Math-Italic", r, t, a.concat(["mathnormal"]));
	if (n === "textord") {
		var b = pi[r][i] && pi[r][i].font;
		if (b === "ams") return ea(i, ha("amsrm", c, l), r, t, a.concat("amsrm", c, l));
		if (b === "main" || !b) return ea(i, ha("textrm", c, l), r, t, a.concat(c, l));
		var x = ha(b, c, l);
		return ea(i, x, r, t, a.concat(x, c, l));
	} else throw Error("unexpected type: " + n + " in makeOrd");
}, ia = (e, t) => {
	if (Ur(e.classes) !== Ur(t.classes) || e.skew !== t.skew || e.maxFontSize !== t.maxFontSize || e.italic !== 0 && e.hasClass("mathnormal")) return !1;
	if (e.classes.length === 1) {
		var n = e.classes[0];
		if (n === "mbin" || n === "mord") return !1;
	}
	for (var r of Object.keys(e.style)) if (e.style[r] !== t.style[r]) return !1;
	for (var i of Object.keys(t.style)) if (e.style[i] !== t.style[i]) return !1;
	return !0;
}, aa = (e) => {
	for (var t = 0; t < e.length - 1; t++) {
		var n = e[t], r = e[t + 1];
		n instanceof $r && r instanceof $r && ia(n, r) && (n.text += r.text, n.height = Math.max(n.height, r.height), n.depth = Math.max(n.depth, r.depth), n.italic = r.italic, e.splice(t + 1, 1), t--);
	}
	return e;
}, oa = function(e) {
	for (var t = 0, n = 0, r = 0, i = 0; i < e.children.length; i++) {
		var a = e.children[i];
		a.height > t && (t = a.height), a.depth > n && (n = a.depth), a.maxFontSize > r && (r = a.maxFontSize);
	}
	e.height = t, e.depth = n, e.maxFontSize = r;
}, K = function(e, t, n, r) {
	var i = new Yr(e, t, n, r);
	return oa(i), i;
}, sa = (e, t, n, r) => new Yr(e, t, n, r), ca = function(e, t, n) {
	var r = K([e], [], t);
	return r.height = Math.max(n || t.fontMetrics().defaultRuleThickness, t.minRuleThickness), r.style.borderBottomWidth = F(r.height), r.maxFontSize = 1, r;
}, la = function(e, t, n, r) {
	var i = new Xr(e, t, n, r);
	return oa(i), i;
}, ua = function(e) {
	var t = new Rr(e);
	return oa(t), t;
}, da = function(e, t) {
	return e instanceof Rr ? K([], [e], t) : e;
}, fa = function(e) {
	if (e.positionType === "individualShift") {
		for (var t = e.children, n = [t[0]], r = -t[0].shift - t[0].elem.depth, i = r, a = 1; a < t.length; a++) {
			var o = -t[a].shift - i - t[a].elem.depth, s = o - (t[a - 1].elem.height + t[a - 1].elem.depth);
			i += o, n.push({
				type: "kern",
				size: s
			}), n.push(t[a]);
		}
		return {
			children: n,
			depth: r
		};
	}
	var c;
	if (e.positionType === "top") {
		for (var l = e.positionData, u = 0; u < e.children.length; u++) {
			var d = e.children[u];
			l -= d.type === "kern" ? d.size : d.elem.height + d.elem.depth;
		}
		c = l;
	} else if (e.positionType === "bottom") c = -e.positionData;
	else {
		var f = e.children[0];
		if (f.type !== "elem") throw Error("First child must have type \"elem\".");
		if (e.positionType === "shift") c = -f.elem.depth - e.positionData;
		else if (e.positionType === "firstBaseline") c = -f.elem.depth;
		else throw Error("Invalid positionType " + e.positionType + ".");
	}
	return {
		children: e.children,
		depth: c
	};
}, pa = function(e, t) {
	for (var { children: n, depth: r } = fa(e), i = 0, a = 0; a < n.length; a++) {
		var o = n[a];
		if (o.type === "elem") {
			var s = o.elem;
			i = Math.max(i, s.maxFontSize, s.height);
		}
	}
	i += 2;
	var c = K(["pstrut"], []);
	c.style.height = F(i);
	for (var l = [], u = r, d = r, f = r, p = 0; p < n.length; p++) {
		var m = n[p];
		if (m.type === "kern") f += m.size;
		else {
			var h = m.elem, g = m.wrapperClasses || [], _ = m.wrapperStyle || {}, v = K(g, [c, h], void 0, _);
			v.style.top = F(-i - f - h.depth), m.marginLeft && (v.style.marginLeft = m.marginLeft), m.marginRight && (v.style.marginRight = m.marginRight), l.push(v), f += h.height + h.depth;
		}
		u = Math.min(u, f), d = Math.max(d, f);
	}
	var y = K(["vlist"], l);
	y.style.height = F(d);
	var b;
	if (u < 0) {
		var x = K(["vlist"], [K([], [])]);
		x.style.height = F(-u), b = [K(["vlist-r"], [y, K(["vlist-s"], [new $r("​")])]), K(["vlist-r"], [x])];
	} else b = [K(["vlist-r"], [y])];
	var S = K(["vlist-t"], b);
	return b.length === 2 && S.classes.push("vlist-t2"), S.height = d, S.depth = -u, S;
}, ma = (e, t) => {
	var n = K(["mspace"], [], t), r = Hr(e, t);
	return n.style.marginRight = F(r), n;
}, ha = (e, t, n) => {
	var r, i;
	switch (e) {
		case "amsrm":
			r = "AMS";
			break;
		case "textrm":
			r = "Main";
			break;
		case "textsf":
			r = "SansSerif";
			break;
		case "texttt":
			r = "Typewriter";
			break;
		default: r = e;
	}
	return i = t === "textbf" && n === "textit" ? "BoldItalic" : t === "textbf" ? "Bold" : n === "textit" ? "Italic" : "Regular", r + "-" + i;
}, ga = {
	mathbf: {
		variant: "bold",
		fontName: "Main-Bold"
	},
	mathrm: {
		variant: "normal",
		fontName: "Main-Regular"
	},
	textit: {
		variant: "italic",
		fontName: "Main-Italic"
	},
	mathit: {
		variant: "italic",
		fontName: "Main-Italic"
	},
	mathnormal: {
		variant: "italic",
		fontName: "Math-Italic"
	},
	mathsfit: {
		variant: "sans-serif-italic",
		fontName: "SansSerif-Italic"
	},
	mathbb: {
		variant: "double-struck",
		fontName: "AMS-Regular"
	},
	mathcal: {
		variant: "script",
		fontName: "Caligraphic-Regular"
	},
	mathfrak: {
		variant: "fraktur",
		fontName: "Fraktur-Regular"
	},
	mathscr: {
		variant: "script",
		fontName: "Script-Regular"
	},
	mathsf: {
		variant: "sans-serif",
		fontName: "SansSerif-Regular"
	},
	mathtt: {
		variant: "monospace",
		fontName: "Typewriter-Regular"
	}
}, _a = {
	vec: [
		"vec",
		.471,
		.714
	],
	oiintSize1: [
		"oiintSize1",
		.957,
		.499
	],
	oiintSize2: [
		"oiintSize2",
		1.472,
		.659
	],
	oiiintSize1: [
		"oiiintSize1",
		1.304,
		.499
	],
	oiiintSize2: [
		"oiiintSize2",
		1.98,
		.659
	]
}, va = function(e, t) {
	var [n, r, i] = _a[e], a = sa(["overlay"], [new ei([new ti(n)], {
		width: F(r),
		height: F(i),
		style: "width:" + F(r),
		viewBox: "0 0 " + 1e3 * r + " " + 1e3 * i,
		preserveAspectRatio: "xMinYMin"
	})], t);
	return a.height = i, a.style.height = F(i), a.style.width = F(r), a;
}, ya = {
	number: 3,
	unit: "mu"
}, ba = {
	number: 4,
	unit: "mu"
}, xa = {
	number: 5,
	unit: "mu"
}, Sa = {
	mord: {
		mop: ya,
		mbin: ba,
		mrel: xa,
		minner: ya
	},
	mop: {
		mord: ya,
		mop: ya,
		mrel: xa,
		minner: ya
	},
	mbin: {
		mord: ba,
		mop: ba,
		mopen: ba,
		minner: ba
	},
	mrel: {
		mord: xa,
		mop: xa,
		mopen: xa,
		minner: xa
	},
	mopen: {},
	mclose: {
		mop: ya,
		mbin: ba,
		mrel: xa,
		minner: ya
	},
	mpunct: {
		mord: ya,
		mop: ya,
		mrel: xa,
		mopen: ya,
		mclose: ya,
		mpunct: ya,
		minner: ya
	},
	minner: {
		mord: ya,
		mop: ya,
		mbin: ba,
		mrel: xa,
		mopen: ya,
		mpunct: ya,
		minner: ya
	}
}, Ca = {
	mord: { mop: ya },
	mop: {
		mord: ya,
		mop: ya
	},
	mbin: {},
	mrel: {},
	mopen: {},
	mclose: { mop: ya },
	mpunct: {},
	minner: { mop: ya }
}, wa = {}, Ta = {}, Ea = {};
function q(e) {
	for (var { type: t, names: n, props: r, handler: i, htmlBuilder: a, mathmlBuilder: o } = e, s = {
		type: t,
		numArgs: r.numArgs,
		argTypes: r.argTypes,
		allowedInArgument: !!r.allowedInArgument,
		allowedInText: !!r.allowedInText,
		allowedInMath: r.allowedInMath === void 0 ? !0 : r.allowedInMath,
		numOptionalArgs: r.numOptionalArgs || 0,
		infix: !!r.infix,
		primitive: !!r.primitive,
		handler: i
	}, c = 0; c < n.length; ++c) wa[n[c]] = s;
	t && (a && (Ta[t] = a), o && (Ea[t] = o));
}
function Da(e) {
	var { type: t, htmlBuilder: n, mathmlBuilder: r } = e;
	q({
		type: t,
		names: [],
		props: { numArgs: 0 },
		handler() {
			throw Error("Should never be called.");
		},
		htmlBuilder: n,
		mathmlBuilder: r
	});
}
var Oa = function(e) {
	return e.type === "ordgroup" && e.body.length === 1 ? e.body[0] : e;
}, ka = function(e) {
	return e.type === "ordgroup" ? e.body : [e];
}, Aa = new Set([
	"leftmost",
	"mbin",
	"mopen",
	"mrel",
	"mop",
	"mpunct"
]), ja = new Set([
	"rightmost",
	"mrel",
	"mclose",
	"mpunct"
]), Ma = {
	display: P.DISPLAY,
	text: P.TEXT,
	script: P.SCRIPT,
	scriptscript: P.SCRIPTSCRIPT
}, Na = {
	mord: "mord",
	mop: "mop",
	mbin: "mbin",
	mrel: "mrel",
	mopen: "mopen",
	mclose: "mclose",
	mpunct: "mpunct",
	minner: "minner"
}, Pa = function(e, t, n, r) {
	r === void 0 && (r = [null, null]);
	for (var i = [], a = 0; a < e.length; a++) {
		var o = Ba(e[a], t);
		if (o instanceof Rr) {
			var s = o.children;
			i.push(...s);
		} else i.push(o);
	}
	if (aa(i), !n) return i;
	var c = t;
	if (e.length === 1) {
		var l = e[0];
		l.type === "sizing" ? c = t.havingSize(l.size) : l.type === "styling" && (c = t.havingStyle(Ma[l.style]));
	}
	var u = K([r[0] || "leftmost"], [], t), d = K([r[1] || "rightmost"], [], t), f = n === "root";
	return Fa(i, (e, t) => {
		var n = t.classes[0], r = e.classes[0];
		n === "mbin" && ja.has(r) ? t.classes[0] = "mord" : r === "mbin" && Aa.has(n) && (e.classes[0] = "mord");
	}, { node: u }, d, f), Fa(i, (e, t) => {
		var n = Ra(t), r = Ra(e), i = n && r ? e.hasClass("mtight") ? Ca[n]?.[r] : Sa[n]?.[r] : null;
		if (i) return ma(i, c);
	}, { node: u }, d, f), i;
}, Fa = function(e, t, n, r, i) {
	r && e.push(r);
	for (var a = 0; a < e.length; a++) {
		var o = e[a], s = Ia(o);
		if (s) {
			Fa(s.children, t, n, null, i);
			continue;
		}
		var c = !o.hasClass("mspace");
		if (c) {
			var l = t(o, n.node);
			l && (n.insertAfter ? n.insertAfter(l) : (e.unshift(l), a++));
		}
		c ? n.node = o : i && o.hasClass("newline") && (n.node = K(["leftmost"])), n.insertAfter = ((t) => (n) => {
			e.splice(t + 1, 0, n), a++;
		})(a);
	}
	r && e.pop();
}, Ia = function(e) {
	return e instanceof Rr || e instanceof Xr || e instanceof Yr && e.hasClass("enclosing") ? e : null;
}, La = function(e, t) {
	var n = Ia(e);
	if (n) {
		var r = n.children;
		if (r.length) {
			if (t === "right") return La(r[r.length - 1], "right");
			if (t === "left") return La(r[0], "left");
		}
	}
	return e;
}, Ra = function(e, t) {
	return e ? (t && (e = La(e, t)), Na[e.classes[0]] || null) : null;
}, za = function(e, t) {
	var n = ["nulldelimiter"].concat(e.baseSizingClasses());
	return K(t.concat(n));
}, Ba = function(e, t, n) {
	if (!e) return K();
	if (Ta[e.type]) {
		var r = Ta[e.type](e, t);
		if (n && t.size !== n.size) {
			r = K(t.sizingClasses(n), [r], t);
			var i = t.sizeMultiplier / n.sizeMultiplier;
			r.height *= i, r.depth *= i;
		}
		return r;
	} else throw new N("Got group of unknown type: '" + e.type + "'");
};
function Va(e, t) {
	var n = K(["base"], e, t), r = K(["strut"]);
	return r.style.height = F(n.height + n.depth), n.depth && (r.style.verticalAlign = F(-n.depth)), n.children.unshift(r), n;
}
function Ha(e, t) {
	var n = null;
	e.length === 1 && e[0].type === "tag" && (n = e[0].tag, e = e[0].body);
	var r = Pa(e, t, "root"), i;
	r.length === 2 && r[1].hasClass("tag") && (i = r.pop());
	for (var a = [], o = [], s = 0; s < r.length; s++) if (o.push(r[s]), r[s].hasClass("mbin") || r[s].hasClass("mrel") || r[s].hasClass("allowbreak")) {
		for (var c = !1; s < r.length - 1 && r[s + 1].hasClass("mspace") && !r[s + 1].hasClass("newline");) s++, o.push(r[s]), r[s].hasClass("nobreak") && (c = !0);
		c || (a.push(Va(o, t)), o = []);
	} else r[s].hasClass("newline") && (o.pop(), o.length > 0 && (a.push(Va(o, t)), o = []), a.push(r[s]));
	o.length > 0 && a.push(Va(o, t));
	var l;
	n ? (l = Va(Pa(n, t, !0), t), l.classes = ["tag"], a.push(l)) : i && a.push(i);
	var u = K(["katex-html"], a);
	if (u.setAttribute("aria-hidden", "true"), l) {
		var d = l.children[0];
		d.style.height = F(u.height + u.depth), u.depth && (d.style.verticalAlign = F(-u.depth));
	}
	return u;
}
function Ua(e) {
	return new Rr(e);
}
var J = class {
	constructor(e, t, n) {
		this.type = void 0, this.attributes = void 0, this.children = void 0, this.classes = void 0, this.type = e, this.attributes = {}, this.children = t || [], this.classes = n || [];
	}
	setAttribute(e, t) {
		this.attributes[e] = t;
	}
	getAttribute(e) {
		return this.attributes[e];
	}
	toNode() {
		var e = document.createElementNS("http://www.w3.org/1998/Math/MathML", this.type);
		for (var t in this.attributes) Object.prototype.hasOwnProperty.call(this.attributes, t) && e.setAttribute(t, this.attributes[t]);
		this.classes.length > 0 && (e.className = Ur(this.classes));
		for (var n = 0; n < this.children.length; n++) if (this.children[n] instanceof Wa && this.children[n + 1] instanceof Wa) {
			for (var r = this.children[n].toText() + this.children[++n].toText(); this.children[n + 1] instanceof Wa;) r += this.children[++n].toText();
			e.appendChild(new Wa(r).toNode());
		} else e.appendChild(this.children[n].toNode());
		return e;
	}
	toMarkup() {
		var e = "<" + this.type;
		for (var t in this.attributes) Object.prototype.hasOwnProperty.call(this.attributes, t) && (e += " " + t + "=\"", e += Jn(this.attributes[t]), e += "\"");
		this.classes.length > 0 && (e += " class =\"" + Jn(Ur(this.classes)) + "\""), e += ">";
		for (var n = 0; n < this.children.length; n++) e += this.children[n].toMarkup();
		return e += "</" + this.type + ">", e;
	}
	toText() {
		return this.children.map((e) => e.toText()).join("");
	}
}, Wa = class {
	constructor(e) {
		this.text = void 0, this.text = e;
	}
	toNode() {
		return document.createTextNode(this.text);
	}
	toMarkup() {
		return Jn(this.toText());
	}
	toText() {
		return this.text;
	}
}, Ga = class {
	constructor(e) {
		this.width = void 0, this.character = void 0, this.width = e, e >= .05555 && e <= .05556 ? this.character = " " : e >= .1666 && e <= .1667 ? this.character = " " : e >= .2222 && e <= .2223 ? this.character = " " : e >= .2777 && e <= .2778 ? this.character = "  " : e >= -.05556 && e <= -.05555 ? this.character = " ⁣" : e >= -.1667 && e <= -.1666 ? this.character = " ⁣" : e >= -.2223 && e <= -.2222 ? this.character = " ⁣" : e >= -.2778 && e <= -.2777 ? this.character = " ⁣" : this.character = null;
	}
	toNode() {
		if (this.character) return document.createTextNode(this.character);
		var e = document.createElementNS("http://www.w3.org/1998/Math/MathML", "mspace");
		return e.setAttribute("width", F(this.width)), e;
	}
	toMarkup() {
		return this.character ? "<mtext>" + this.character + "</mtext>" : "<mspace width=\"" + F(this.width) + "\"/>";
	}
	toText() {
		return this.character ? this.character : " ";
	}
}, Ka = new Set(["\\imath", "\\jmath"]), qa = new Set(["mrow", "mtable"]), Ja = function(e, t, n) {
	return pi[t][e] && pi[t][e].replace && e.charCodeAt(0) !== 55349 && !(xi.hasOwnProperty(e) && n && (n.fontFamily && n.fontFamily.slice(4, 6) === "tt" || n.font && n.font.slice(4, 6) === "tt")) && (e = pi[t][e].replace), new Wa(e);
}, Ya = function(e) {
	return e.length === 1 ? e[0] : new J("mrow", e);
}, Xa = {
	mathit: "italic",
	boldsymbol: (e) => e.type === "textord" ? "bold" : "bold-italic",
	mathbf: "bold",
	mathbb: "double-struck",
	mathsfit: "sans-serif-italic",
	mathfrak: "fraktur",
	mathscr: "script",
	mathcal: "script",
	mathsf: "sans-serif",
	mathtt: "monospace"
}, Za = (e, t) => {
	if (e.mode === "text") {
		if (t.fontFamily === "texttt") return "monospace";
		if (t.fontFamily === "textsf") return t.fontShape === "textit" && t.fontWeight === "textbf" ? "sans-serif-bold-italic" : t.fontShape === "textit" ? "sans-serif-italic" : t.fontWeight === "textbf" ? "bold-sans-serif" : "sans-serif";
		if (t.fontShape === "textit" && t.fontWeight === "textbf") return "bold-italic";
		if (t.fontShape === "textit") return "italic";
		if (t.fontWeight === "textbf") return "bold";
	}
	var n = t.font;
	if (!n || n === "mathnormal") return null;
	var r = e.mode, i = Xa[n];
	if (i) return typeof i == "function" ? i(e) : i;
	var a = e.text;
	if (Ka.has(a)) return null;
	if (pi[r][a]) {
		var o = pi[r][a].replace;
		o && (a = o);
	}
	var s = ga[n].fontName;
	return ui(a, s, r) ? ga[n].variant : null;
};
function Qa(e) {
	if (!e) return !1;
	if (e.type === "mi" && e.children.length === 1) {
		var t = e.children[0];
		return t instanceof Wa && t.text === ".";
	} else if (e.type === "mo" && e.children.length === 1 && e.getAttribute("separator") === "true" && e.getAttribute("lspace") === "0em" && e.getAttribute("rspace") === "0em") {
		var n = e.children[0];
		return n instanceof Wa && n.text === ",";
	} else return !1;
}
var $a = function(e, t, n) {
	if (e.length === 1) {
		var r = Y(e[0], t);
		return n && r instanceof J && r.type === "mo" && (r.setAttribute("lspace", "0em"), r.setAttribute("rspace", "0em")), [r];
	}
	for (var i = [], a, o = 0; o < e.length; o++) {
		var s = Y(e[o], t);
		if (s instanceof J && a instanceof J) {
			if (s.type === "mtext" && a.type === "mtext" && s.getAttribute("mathvariant") === a.getAttribute("mathvariant")) {
				a.children.push(...s.children);
				continue;
			} else if (s.type === "mn" && a.type === "mn") {
				a.children.push(...s.children);
				continue;
			} else if (Qa(s) && a.type === "mn") {
				a.children.push(...s.children);
				continue;
			} else if (s.type === "mn" && Qa(a)) s.children = [...a.children, ...s.children], i.pop();
			else if ((s.type === "msup" || s.type === "msub") && s.children.length >= 1 && (a.type === "mn" || Qa(a))) {
				var c = s.children[0];
				c instanceof J && c.type === "mn" && (c.children = [...a.children, ...c.children], i.pop());
			} else if (a.type === "mi" && a.children.length === 1) {
				var l = a.children[0];
				if (l instanceof Wa && l.text === "̸" && (s.type === "mo" || s.type === "mi" || s.type === "mn")) {
					var u = s.children[0];
					u instanceof Wa && u.text.length > 0 && (u.text = u.text.slice(0, 1) + "̸" + u.text.slice(1), i.pop());
				}
			}
		}
		i.push(s), a = s;
	}
	return i;
}, eo = function(e, t, n) {
	return Ya($a(e, t, n));
}, Y = function(e, t) {
	if (!e) return new J("mrow");
	if (Ea[e.type]) return Ea[e.type](e, t);
	throw new N("Got group of unknown type: '" + e.type + "'");
};
function to(e, t, n, r, i) {
	var a = $a(e, n), o = a.length === 1 && a[0] instanceof J && qa.has(a[0].type) ? a[0] : new J("mrow", a), s = new J("annotation", [new Wa(t)]);
	s.setAttribute("encoding", "application/x-tex");
	var c = new J("math", [new J("semantics", [o, s])]);
	return c.setAttribute("xmlns", "http://www.w3.org/1998/Math/MathML"), r && c.setAttribute("display", "block"), K([i ? "katex" : "katex-mathml"], [c]);
}
var no = [
	[
		1,
		1,
		1
	],
	[
		2,
		1,
		1
	],
	[
		3,
		1,
		1
	],
	[
		4,
		2,
		1
	],
	[
		5,
		2,
		1
	],
	[
		6,
		3,
		1
	],
	[
		7,
		4,
		2
	],
	[
		8,
		6,
		3
	],
	[
		9,
		7,
		6
	],
	[
		10,
		8,
		7
	],
	[
		11,
		10,
		9
	]
], X = [
	.5,
	.6,
	.7,
	.8,
	.9,
	1,
	1.2,
	1.44,
	1.728,
	2.074,
	2.488
], ro = function(e, t) {
	return t.size < 2 ? e : no[e - 1][t.size - 1];
}, io = class e {
	constructor(t) {
		this.style = void 0, this.color = void 0, this.size = void 0, this.textSize = void 0, this.phantom = void 0, this.font = void 0, this.fontFamily = void 0, this.fontWeight = void 0, this.fontShape = void 0, this.sizeMultiplier = void 0, this.maxSize = void 0, this.minRuleThickness = void 0, this._fontMetrics = void 0, this.style = t.style, this.color = t.color, this.size = t.size || e.BASESIZE, this.textSize = t.textSize || this.size, this.phantom = !!t.phantom, this.font = t.font || "", this.fontFamily = t.fontFamily || "", this.fontWeight = t.fontWeight || "", this.fontShape = t.fontShape || "", this.sizeMultiplier = X[this.size - 1], this.maxSize = t.maxSize, this.minRuleThickness = t.minRuleThickness, this._fontMetrics = void 0;
	}
	extend(t) {
		var n = {
			style: this.style,
			size: this.size,
			textSize: this.textSize,
			color: this.color,
			phantom: this.phantom,
			font: this.font,
			fontFamily: this.fontFamily,
			fontWeight: this.fontWeight,
			fontShape: this.fontShape,
			maxSize: this.maxSize,
			minRuleThickness: this.minRuleThickness
		};
		return Object.assign(n, t), new e(n);
	}
	havingStyle(e) {
		return this.style === e ? this : this.extend({
			style: e,
			size: ro(this.textSize, e)
		});
	}
	havingCrampedStyle() {
		return this.havingStyle(this.style.cramp());
	}
	havingSize(e) {
		return this.size === e && this.textSize === e ? this : this.extend({
			style: this.style.text(),
			size: e,
			textSize: e,
			sizeMultiplier: X[e - 1]
		});
	}
	havingBaseStyle(t) {
		t ||= this.style.text();
		var n = ro(e.BASESIZE, t);
		return this.size === n && this.textSize === e.BASESIZE && this.style === t ? this : this.extend({
			style: t,
			size: n
		});
	}
	havingBaseSizing() {
		var e;
		switch (this.style.id) {
			case 4:
			case 5:
				e = 3;
				break;
			case 6:
			case 7:
				e = 1;
				break;
			default: e = 6;
		}
		return this.extend({
			style: this.style.text(),
			size: e
		});
	}
	withColor(e) {
		return this.extend({ color: e });
	}
	withPhantom() {
		return this.extend({ phantom: !0 });
	}
	withFont(e) {
		return this.extend({ font: e });
	}
	withTextFontFamily(e) {
		return this.extend({
			fontFamily: e,
			font: ""
		});
	}
	withTextFontWeight(e) {
		return this.extend({
			fontWeight: e,
			font: ""
		});
	}
	withTextFontShape(e) {
		return this.extend({
			fontShape: e,
			font: ""
		});
	}
	sizingClasses(e) {
		return e.size === this.size ? [] : [
			"sizing",
			"reset-size" + e.size,
			"size" + this.size
		];
	}
	baseSizingClasses() {
		return this.size === e.BASESIZE ? [] : [
			"sizing",
			"reset-size" + this.size,
			"size" + e.BASESIZE
		];
	}
	fontMetrics() {
		return this._fontMetrics ||= fi(this.size), this._fontMetrics;
	}
	getColor() {
		return this.phantom ? "transparent" : this.color;
	}
};
io.BASESIZE = 6;
var ao = function(e) {
	return new io({
		style: e.displayMode ? P.DISPLAY : P.TEXT,
		maxSize: e.maxSize,
		minRuleThickness: e.minRuleThickness
	});
}, oo = function(e, t) {
	if (t.displayMode) {
		var n = ["katex-display"];
		t.leqno && n.push("leqno"), t.fleqn && n.push("fleqn"), e = K(n, [e]);
	}
	return e;
}, so = function(e, t, n) {
	var r = ao(n), i;
	return n.output === "mathml" ? to(e, t, r, n.displayMode, !0) : (i = n.output === "html" ? K(["katex"], [Ha(e, r)]) : K(["katex"], [to(e, t, r, n.displayMode, !1), Ha(e, r)]), oo(i, n));
}, co = function(e, t, n) {
	return oo(K(["katex"], [Ha(e, ao(n))]), n);
}, lo = {
	widehat: "^",
	widecheck: "ˇ",
	widetilde: "~",
	utilde: "~",
	overleftarrow: "←",
	underleftarrow: "←",
	xleftarrow: "←",
	overrightarrow: "→",
	underrightarrow: "→",
	xrightarrow: "→",
	underbrace: "⏟",
	overbrace: "⏞",
	underbracket: "⎵",
	overbracket: "⎴",
	overgroup: "⏠",
	undergroup: "⏡",
	overleftrightarrow: "↔",
	underleftrightarrow: "↔",
	xleftrightarrow: "↔",
	Overrightarrow: "⇒",
	xRightarrow: "⇒",
	overleftharpoon: "↼",
	xleftharpoonup: "↼",
	overrightharpoon: "⇀",
	xrightharpoonup: "⇀",
	xLeftarrow: "⇐",
	xLeftrightarrow: "⇔",
	xhookleftarrow: "↩",
	xhookrightarrow: "↪",
	xmapsto: "↦",
	xrightharpoondown: "⇁",
	xleftharpoondown: "↽",
	xrightleftharpoons: "⇌",
	xleftrightharpoons: "⇋",
	xtwoheadleftarrow: "↞",
	xtwoheadrightarrow: "↠",
	xlongequal: "=",
	xtofrom: "⇄",
	xrightleftarrows: "⇄",
	xrightequilibrium: "⇌",
	xleftequilibrium: "⇋",
	"\\cdrightarrow": "→",
	"\\cdleftarrow": "←",
	"\\cdlongequal": "="
}, uo = function(e) {
	var t = new J("mo", [new Wa(lo[e.replace(/^\\/, "")])]);
	return t.setAttribute("stretchy", "true"), t;
}, fo = {
	overrightarrow: [
		["rightarrow"],
		.888,
		522,
		"xMaxYMin"
	],
	overleftarrow: [
		["leftarrow"],
		.888,
		522,
		"xMinYMin"
	],
	underrightarrow: [
		["rightarrow"],
		.888,
		522,
		"xMaxYMin"
	],
	underleftarrow: [
		["leftarrow"],
		.888,
		522,
		"xMinYMin"
	],
	xrightarrow: [
		["rightarrow"],
		1.469,
		522,
		"xMaxYMin"
	],
	"\\cdrightarrow": [
		["rightarrow"],
		3,
		522,
		"xMaxYMin"
	],
	xleftarrow: [
		["leftarrow"],
		1.469,
		522,
		"xMinYMin"
	],
	"\\cdleftarrow": [
		["leftarrow"],
		3,
		522,
		"xMinYMin"
	],
	Overrightarrow: [
		["doublerightarrow"],
		.888,
		560,
		"xMaxYMin"
	],
	xRightarrow: [
		["doublerightarrow"],
		1.526,
		560,
		"xMaxYMin"
	],
	xLeftarrow: [
		["doubleleftarrow"],
		1.526,
		560,
		"xMinYMin"
	],
	overleftharpoon: [
		["leftharpoon"],
		.888,
		522,
		"xMinYMin"
	],
	xleftharpoonup: [
		["leftharpoon"],
		.888,
		522,
		"xMinYMin"
	],
	xleftharpoondown: [
		["leftharpoondown"],
		.888,
		522,
		"xMinYMin"
	],
	overrightharpoon: [
		["rightharpoon"],
		.888,
		522,
		"xMaxYMin"
	],
	xrightharpoonup: [
		["rightharpoon"],
		.888,
		522,
		"xMaxYMin"
	],
	xrightharpoondown: [
		["rightharpoondown"],
		.888,
		522,
		"xMaxYMin"
	],
	xlongequal: [
		["longequal"],
		.888,
		334,
		"xMinYMin"
	],
	"\\cdlongequal": [
		["longequal"],
		3,
		334,
		"xMinYMin"
	],
	xtwoheadleftarrow: [
		["twoheadleftarrow"],
		.888,
		334,
		"xMinYMin"
	],
	xtwoheadrightarrow: [
		["twoheadrightarrow"],
		.888,
		334,
		"xMaxYMin"
	],
	overleftrightarrow: [
		["leftarrow", "rightarrow"],
		.888,
		522
	],
	overbrace: [
		[
			"leftbrace",
			"midbrace",
			"rightbrace"
		],
		1.6,
		548
	],
	underbrace: [
		[
			"leftbraceunder",
			"midbraceunder",
			"rightbraceunder"
		],
		1.6,
		548
	],
	underleftrightarrow: [
		["leftarrow", "rightarrow"],
		.888,
		522
	],
	xleftrightarrow: [
		["leftarrow", "rightarrow"],
		1.75,
		522
	],
	xLeftrightarrow: [
		["doubleleftarrow", "doublerightarrow"],
		1.75,
		560
	],
	xrightleftharpoons: [
		["leftharpoondownplus", "rightharpoonplus"],
		1.75,
		716
	],
	xleftrightharpoons: [
		["leftharpoonplus", "rightharpoondownplus"],
		1.75,
		716
	],
	xhookleftarrow: [
		["leftarrow", "righthook"],
		1.08,
		522
	],
	xhookrightarrow: [
		["lefthook", "rightarrow"],
		1.08,
		522
	],
	overlinesegment: [
		["leftlinesegment", "rightlinesegment"],
		.888,
		522
	],
	underlinesegment: [
		["leftlinesegment", "rightlinesegment"],
		.888,
		522
	],
	overbracket: [
		["leftbracketover", "rightbracketover"],
		1.6,
		440
	],
	underbracket: [
		["leftbracketunder", "rightbracketunder"],
		1.6,
		410
	],
	overgroup: [
		["leftgroup", "rightgroup"],
		.888,
		342
	],
	undergroup: [
		["leftgroupunder", "rightgroupunder"],
		.888,
		342
	],
	xmapsto: [
		["leftmapsto", "rightarrow"],
		1.5,
		522
	],
	xtofrom: [
		["leftToFrom", "rightToFrom"],
		1.75,
		528
	],
	xrightleftarrows: [
		["baraboveleftarrow", "rightarrowabovebar"],
		1.75,
		901
	],
	xrightequilibrium: [
		["baraboveshortleftharpoon", "rightharpoonaboveshortbar"],
		1.75,
		716
	],
	xleftequilibrium: [
		["shortbaraboveleftharpoon", "shortrightharpoonabovebar"],
		1.75,
		716
	]
}, po = new Set([
	"widehat",
	"widecheck",
	"widetilde",
	"utilde"
]), mo = function(e, t) {
	function n() {
		var n = 4e5, r = e.label.slice(1);
		if (po.has(r) && "base" in e) {
			var i = e.base.type === "ordgroup" ? e.base.body.length : 1, a, o, s;
			if (i > 5) r === "widehat" || r === "widecheck" ? (a = 420, n = 2364, s = .42, o = r + "4") : (a = 312, n = 2340, s = .34, o = "tilde4");
			else {
				var c = [
					1,
					1,
					2,
					2,
					3,
					3
				][i];
				r === "widehat" || r === "widecheck" ? (n = [
					0,
					1062,
					2364,
					2364,
					2364
				][c], a = [
					0,
					239,
					300,
					360,
					420
				][c], s = [
					0,
					.24,
					.3,
					.3,
					.36,
					.42
				][c], o = r + c) : (n = [
					0,
					600,
					1033,
					2339,
					2340
				][c], a = [
					0,
					260,
					286,
					306,
					312
				][c], s = [
					0,
					.26,
					.286,
					.3,
					.306,
					.34
				][c], o = "tilde" + c);
			}
			return {
				span: sa([], [new ei([new ti(o)], {
					width: "100%",
					height: F(s),
					viewBox: "0 0 " + n + " " + a,
					preserveAspectRatio: "none"
				})], t),
				minWidth: 0,
				height: s
			};
		} else {
			var l = [], u = fo[r];
			if (!u) throw Error("No SVG data for \"" + r + "\".");
			var [d, f, p] = u, m = p / 1e3, h = d.length, g, _;
			if (h === 1) {
				if (u.length !== 4) throw Error("Expected 4-tuple for single-path SVG data \"" + r + "\".");
				g = ["hide-tail"], _ = [u[3]];
			} else if (h === 2) g = ["halfarrow-left", "halfarrow-right"], _ = ["xMinYMin", "xMaxYMin"];
			else if (h === 3) g = [
				"brace-left",
				"brace-center",
				"brace-right"
			], _ = [
				"xMinYMin",
				"xMidYMin",
				"xMaxYMin"
			];
			else throw Error("Correct katexImagesData or update code here to support\n                    " + h + " children.");
			for (var v = 0; v < h; v++) {
				var y = new ei([new ti(d[v])], {
					width: "400em",
					height: F(m),
					viewBox: "0 0 " + n + " " + p,
					preserveAspectRatio: _[v] + " slice"
				}), b = sa([g[v]], [y], t);
				if (h === 1) return {
					span: b,
					minWidth: f,
					height: m
				};
				b.style.height = F(m), l.push(b);
			}
			return {
				span: K(["stretchy"], l, t),
				minWidth: f,
				height: m
			};
		}
	}
	var { span: r, minWidth: i, height: a } = n();
	return r.height = a, r.style.height = F(a), i > 0 && (r.style.minWidth = F(i)), r;
}, ho = function(e, t, n, r, i) {
	var a, o = e.height + e.depth + n + r;
	if (/fbox|color|angl/.test(t)) {
		if (a = K(["stretchy", t], [], i), t === "fbox") {
			var s = i.color && i.getColor();
			s && (a.style.borderColor = s);
		}
	} else {
		var c = [];
		/^[bx]cancel$/.test(t) && c.push(new ni({
			x1: "0",
			y1: "0",
			x2: "100%",
			y2: "100%",
			"stroke-width": "0.046em"
		})), /^x?cancel$/.test(t) && c.push(new ni({
			x1: "0",
			y1: "100%",
			x2: "100%",
			y2: "0",
			"stroke-width": "0.046em"
		})), a = sa([], [new ei(c, {
			width: "100%",
			height: F(o)
		})], i);
	}
	return a.height = o, a.style.height = F(o), a;
}, go = {
	bin: 1,
	close: 1,
	inner: 1,
	open: 1,
	punct: 1,
	rel: 1
}, _o = {
	"accent-token": 1,
	mathord: 1,
	"op-token": 1,
	spacing: 1,
	textord: 1
};
function vo(e) {
	return e in go;
}
function Z(e, t) {
	if (!e || e.type !== t) throw Error("Expected node of type " + t + ", but got " + (e ? "node of type " + e.type : String(e)));
	return e;
}
function yo(e) {
	var t = bo(e);
	if (!t) throw Error("Expected node of symbol group type, but got " + (e ? "node of type " + e.type : String(e)));
	return t;
}
function bo(e) {
	return e && (e.type === "atom" || _o.hasOwnProperty(e.type)) ? e : null;
}
var xo = (e) => {
	if (e instanceof $r) return e;
	if (ai(e) && e.children.length === 1) return xo(e.children[0]);
}, So = (e, t) => {
	var n, r, i;
	e && e.type === "supsub" ? (r = Z(e.base, "accent"), n = r.base, e.base = n, i = ii(Ba(e, t)), e.base = r) : (r = Z(e, "accent"), n = r.base);
	var a = Ba(n, t.havingCrampedStyle()), o = r.isShifty && Zn(n), s = 0;
	o && (s = xo(a)?.skew ?? 0);
	var c = r.label === "\\c", l = c ? a.height + a.depth : Math.min(a.height, t.fontMetrics().xHeight), u;
	if (r.isStretchy) u = mo(r, t), u = pa({
		positionType: "firstBaseline",
		children: [{
			type: "elem",
			elem: a
		}, {
			type: "elem",
			elem: u,
			wrapperClasses: ["svg-align"],
			wrapperStyle: s > 0 ? {
				width: "calc(100% - " + F(2 * s) + ")",
				marginLeft: F(2 * s)
			} : void 0
		}]
	});
	else {
		var d, f;
		r.label === "\\vec" ? (d = va("vec", t), f = _a.vec[1]) : (d = ra({
			type: "textord",
			mode: r.mode,
			text: r.label
		}, t, "textord"), d = ri(d), d.italic = 0, f = d.width, c && (l += d.depth)), u = K(["accent-body"], [d]);
		var p = r.label === "\\textcircled";
		p && (u.classes.push("accent-full"), l = a.height);
		var m = s;
		p || (m -= f / 2), u.style.left = F(m), r.label === "\\textcircled" && (u.style.top = ".2em"), u = pa({
			positionType: "firstBaseline",
			children: [
				{
					type: "elem",
					elem: a
				},
				{
					type: "kern",
					size: -l
				},
				{
					type: "elem",
					elem: u
				}
			]
		});
	}
	var h = K(["mord", "accent"], [u], t);
	return i ? (i.children[0] = h, i.height = Math.max(h.height, i.height), i.classes[0] = "mord", i) : h;
}, Co = (e, t) => {
	var n = e.isStretchy ? uo(e.label) : new J("mo", [Ja(e.label, e.mode)]), r = new J("mover", [Y(e.base, t), n]);
	return r.setAttribute("accent", "true"), r;
}, wo = new RegExp([
	"\\acute",
	"\\grave",
	"\\ddot",
	"\\tilde",
	"\\bar",
	"\\breve",
	"\\check",
	"\\hat",
	"\\vec",
	"\\dot",
	"\\mathring"
].map((e) => "\\" + e).join("|"));
q({
	type: "accent",
	names: [
		"\\acute",
		"\\grave",
		"\\ddot",
		"\\tilde",
		"\\bar",
		"\\breve",
		"\\check",
		"\\hat",
		"\\vec",
		"\\dot",
		"\\mathring",
		"\\widecheck",
		"\\widehat",
		"\\widetilde",
		"\\overrightarrow",
		"\\overleftarrow",
		"\\Overrightarrow",
		"\\overleftrightarrow",
		"\\overgroup",
		"\\overlinesegment",
		"\\overleftharpoon",
		"\\overrightharpoon"
	],
	props: { numArgs: 1 },
	handler: (e, t) => {
		var n = Oa(t[0]), r = !wo.test(e.funcName), i = !r || e.funcName === "\\widehat" || e.funcName === "\\widetilde" || e.funcName === "\\widecheck";
		return {
			type: "accent",
			mode: e.parser.mode,
			label: e.funcName,
			isStretchy: r,
			isShifty: i,
			base: n
		};
	},
	htmlBuilder: So,
	mathmlBuilder: Co
}), q({
	type: "accent",
	names: [
		"\\'",
		"\\`",
		"\\^",
		"\\~",
		"\\=",
		"\\u",
		"\\.",
		"\\\"",
		"\\c",
		"\\r",
		"\\H",
		"\\v",
		"\\textcircled"
	],
	props: {
		numArgs: 1,
		allowedInText: !0,
		allowedInMath: !0,
		argTypes: ["primitive"]
	},
	handler: (e, t) => {
		var n = t[0], r = e.parser.mode;
		return r === "math" && (e.parser.settings.reportNonstrict("mathVsTextAccents", "LaTeX's accent " + e.funcName + " works only in text mode"), r = "text"), {
			type: "accent",
			mode: r,
			label: e.funcName,
			isStretchy: !1,
			isShifty: !0,
			base: n
		};
	},
	htmlBuilder: So,
	mathmlBuilder: Co
}), q({
	type: "accentUnder",
	names: [
		"\\underleftarrow",
		"\\underrightarrow",
		"\\underleftrightarrow",
		"\\undergroup",
		"\\underlinesegment",
		"\\utilde"
	],
	props: { numArgs: 1 },
	handler: (e, t) => {
		var { parser: n, funcName: r } = e, i = t[0];
		return {
			type: "accentUnder",
			mode: n.mode,
			label: r,
			base: i
		};
	},
	htmlBuilder: (e, t) => {
		var n = Ba(e.base, t), r = mo(e, t), i = e.label === "\\utilde" ? .12 : 0;
		return K(["mord", "accentunder"], [pa({
			positionType: "top",
			positionData: n.height,
			children: [
				{
					type: "elem",
					elem: r,
					wrapperClasses: ["svg-align"]
				},
				{
					type: "kern",
					size: i
				},
				{
					type: "elem",
					elem: n
				}
			]
		})], t);
	},
	mathmlBuilder: (e, t) => {
		var n = uo(e.label), r = new J("munder", [Y(e.base, t), n]);
		return r.setAttribute("accentunder", "true"), r;
	}
});
var To = (e) => {
	var t = new J("mpadded", e ? [e] : []);
	return t.setAttribute("width", "+0.6em"), t.setAttribute("lspace", "0.3em"), t;
};
q({
	type: "xArrow",
	names: [
		"\\xleftarrow",
		"\\xrightarrow",
		"\\xLeftarrow",
		"\\xRightarrow",
		"\\xleftrightarrow",
		"\\xLeftrightarrow",
		"\\xhookleftarrow",
		"\\xhookrightarrow",
		"\\xmapsto",
		"\\xrightharpoondown",
		"\\xrightharpoonup",
		"\\xleftharpoondown",
		"\\xleftharpoonup",
		"\\xrightleftharpoons",
		"\\xleftrightharpoons",
		"\\xlongequal",
		"\\xtwoheadrightarrow",
		"\\xtwoheadleftarrow",
		"\\xtofrom",
		"\\xrightleftarrows",
		"\\xrightequilibrium",
		"\\xleftequilibrium",
		"\\\\cdrightarrow",
		"\\\\cdleftarrow",
		"\\\\cdlongequal"
	],
	props: {
		numArgs: 1,
		numOptionalArgs: 1
	},
	handler(e, t, n) {
		var { parser: r, funcName: i } = e;
		return {
			type: "xArrow",
			mode: r.mode,
			label: i,
			body: t[0],
			below: n[0]
		};
	},
	htmlBuilder(e, t) {
		var n = t.style, r = t.havingStyle(n.sup()), i = da(Ba(e.body, r, t), t), a = e.label.slice(0, 2) === "\\x" ? "x" : "cd";
		i.classes.push(a + "-arrow-pad");
		var o;
		e.below && (r = t.havingStyle(n.sub()), o = da(Ba(e.below, r, t), t), o.classes.push(a + "-arrow-pad"));
		var s = mo(e, t), c = -t.fontMetrics().axisHeight + .5 * s.height, l = -t.fontMetrics().axisHeight - .5 * s.height - .111;
		(i.depth > .25 || e.label === "\\xleftequilibrium") && (l -= i.depth);
		var u;
		if (o) {
			var d = -t.fontMetrics().axisHeight + o.height + .5 * s.height + .111;
			u = pa({
				positionType: "individualShift",
				children: [
					{
						type: "elem",
						elem: i,
						shift: l
					},
					{
						type: "elem",
						elem: s,
						shift: c,
						wrapperClasses: ["svg-align"]
					},
					{
						type: "elem",
						elem: o,
						shift: d
					}
				]
			});
		} else u = pa({
			positionType: "individualShift",
			children: [{
				type: "elem",
				elem: i,
				shift: l
			}, {
				type: "elem",
				elem: s,
				shift: c,
				wrapperClasses: ["svg-align"]
			}]
		});
		return K(["mrel", "x-arrow"], [u], t);
	},
	mathmlBuilder(e, t) {
		var n = uo(e.label);
		n.setAttribute("minsize", e.label.charAt(0) === "x" ? "1.75em" : "3.0em");
		var r;
		if (e.body) {
			var i = To(Y(e.body, t));
			r = e.below ? new J("munderover", [
				n,
				To(Y(e.below, t)),
				i
			]) : new J("mover", [n, i]);
		} else e.below ? r = new J("munder", [n, To(Y(e.below, t))]) : (r = To(), r = new J("mover", [n, r]));
		return r;
	}
});
function Eo(e, t) {
	var n = Pa(e.body, t, !0);
	return K([e.mclass], n, t);
}
function Do(e, t) {
	var n, r = $a(e.body, t);
	return e.mclass === "minner" ? n = new J("mpadded", r) : e.mclass === "mord" ? e.isCharacterBox ? (n = r[0], n.type = "mi") : n = new J("mi", r) : (e.isCharacterBox ? (n = r[0], n.type = "mo") : n = new J("mo", r), e.mclass === "mbin" ? (n.attributes.lspace = "0.22em", n.attributes.rspace = "0.22em") : e.mclass === "mpunct" ? (n.attributes.lspace = "0em", n.attributes.rspace = "0.17em") : e.mclass === "mopen" || e.mclass === "mclose" ? (n.attributes.lspace = "0em", n.attributes.rspace = "0em") : e.mclass === "minner" && (n.attributes.lspace = "0.0556em", n.attributes.width = "+0.1111em")), n;
}
q({
	type: "mclass",
	names: [
		"\\mathord",
		"\\mathbin",
		"\\mathrel",
		"\\mathopen",
		"\\mathclose",
		"\\mathpunct",
		"\\mathinner"
	],
	props: {
		numArgs: 1,
		primitive: !0
	},
	handler(e, t) {
		var { parser: n, funcName: r } = e, i = t[0];
		return {
			type: "mclass",
			mode: n.mode,
			mclass: "m" + r.slice(5),
			body: ka(i),
			isCharacterBox: Zn(i)
		};
	},
	htmlBuilder: Eo,
	mathmlBuilder: Do
});
var Oo = (e) => {
	var t = e.type === "ordgroup" && e.body.length ? e.body[0] : e;
	return t.type === "atom" && (t.family === "bin" || t.family === "rel") ? "m" + t.family : "mord";
};
q({
	type: "mclass",
	names: ["\\@binrel"],
	props: { numArgs: 2 },
	handler(e, t) {
		var { parser: n } = e;
		return {
			type: "mclass",
			mode: n.mode,
			mclass: Oo(t[0]),
			body: ka(t[1]),
			isCharacterBox: Zn(t[1])
		};
	}
}), q({
	type: "mclass",
	names: [
		"\\stackrel",
		"\\overset",
		"\\underset"
	],
	props: { numArgs: 2 },
	handler(e, t) {
		var { parser: n, funcName: r } = e, i = t[1], a = t[0], o = r === "\\stackrel" ? "mrel" : Oo(i), s = {
			type: "op",
			mode: i.mode,
			limits: !0,
			alwaysHandleSupSub: !0,
			parentIsSupSub: !1,
			symbol: !1,
			suppressBaseShift: r !== "\\stackrel",
			body: ka(i)
		}, c = {
			type: "supsub",
			mode: a.mode,
			base: s,
			sup: r === "\\underset" ? null : a,
			sub: r === "\\underset" ? a : null
		};
		return {
			type: "mclass",
			mode: n.mode,
			mclass: o,
			body: [c],
			isCharacterBox: Zn(c)
		};
	},
	htmlBuilder: Eo,
	mathmlBuilder: Do
}), q({
	type: "pmb",
	names: ["\\pmb"],
	props: {
		numArgs: 1,
		allowedInText: !0
	},
	handler(e, t) {
		var { parser: n } = e;
		return {
			type: "pmb",
			mode: n.mode,
			mclass: Oo(t[0]),
			body: ka(t[0])
		};
	},
	htmlBuilder(e, t) {
		var n = Pa(e.body, t, !0), r = K([e.mclass], n, t);
		return r.style.textShadow = "0.02em 0.01em 0.04px", r;
	},
	mathmlBuilder(e, t) {
		var n = new J("mstyle", $a(e.body, t));
		return n.setAttribute("style", "text-shadow: 0.02em 0.01em 0.04px"), n;
	}
});
var ko = {
	">": "\\\\cdrightarrow",
	"<": "\\\\cdleftarrow",
	"=": "\\\\cdlongequal",
	A: "\\uparrow",
	V: "\\downarrow",
	"|": "\\Vert",
	".": "no arrow"
}, Ao = () => ({
	type: "styling",
	body: [],
	mode: "math",
	style: "display",
	resetFont: !0
}), jo = (e) => e.type === "textord" && e.text === "@", Mo = (e, t) => (e.type === "mathord" || e.type === "atom") && e.text === t;
function No(e, t, n) {
	var r = ko[e];
	switch (r) {
		case "\\\\cdrightarrow":
		case "\\\\cdleftarrow": return n.callFunction(r, [t[0]], [t[1]]);
		case "\\uparrow":
		case "\\downarrow":
			var i = n.callFunction("\\\\cdleft", [t[0]], []), a = {
				type: "atom",
				text: r,
				mode: "math",
				family: "rel"
			}, o = {
				type: "ordgroup",
				mode: "math",
				body: [
					i,
					n.callFunction("\\Big", [a], []),
					n.callFunction("\\\\cdright", [t[1]], [])
				]
			};
			return n.callFunction("\\\\cdparent", [o], []);
		case "\\\\cdlongequal": return n.callFunction("\\\\cdlongequal", [], []);
		case "\\Vert": return n.callFunction("\\Big", [{
			type: "textord",
			text: "\\Vert",
			mode: "math"
		}], []);
		default: return {
			type: "textord",
			text: " ",
			mode: "math"
		};
	}
}
function Po(e) {
	var t = [];
	for (e.gullet.beginGroup(), e.gullet.macros.set("\\cr", "\\\\\\relax"), e.gullet.beginGroup();;) {
		t.push(e.parseExpression(!1, "\\\\")), e.gullet.endGroup(), e.gullet.beginGroup();
		var n = e.fetch().text;
		if (n === "&" || n === "\\\\") e.consume();
		else if (n === "\\end") {
			t[t.length - 1].length === 0 && t.pop();
			break;
		} else throw new N("Expected \\\\ or \\cr or \\end", e.nextToken);
	}
	for (var r = [], i = [r], a = 0; a < t.length; a++) {
		for (var o = t[a], s = Ao(), c = 0; c < o.length; c++) if (!jo(o[c])) s.body.push(o[c]);
		else {
			r.push(s), c += 1;
			var l = yo(o[c]).text, u = [, ,];
			if (u[0] = {
				type: "ordgroup",
				mode: "math",
				body: []
			}, u[1] = {
				type: "ordgroup",
				mode: "math",
				body: []
			}, !"=|.".includes(l)) if ("<>AV".includes(l)) for (var d = 0; d < 2; d++) {
				for (var f = !0, p = c + 1; p < o.length; p++) {
					if (Mo(o[p], l)) {
						f = !1, c = p;
						break;
					}
					if (jo(o[p])) throw new N("Missing a " + l + " character to complete a CD arrow.", o[p]);
					u[d].body.push(o[p]);
				}
				if (f) throw new N("Missing a " + l + " character to complete a CD arrow.", o[c]);
			}
			else throw new N("Expected one of \"<>AV=|.\" after @", o[c]);
			var m = {
				type: "styling",
				body: [No(l, u, e)],
				mode: "math",
				style: "display",
				resetFont: !0
			};
			r.push(m), s = Ao();
		}
		a % 2 == 0 ? r.push(s) : r.shift(), r = [], i.push(r);
	}
	return e.gullet.endGroup(), e.gullet.endGroup(), {
		type: "array",
		mode: "math",
		body: i,
		arraystretch: 1,
		addJot: !0,
		rowGaps: [null],
		cols: Array(i[0].length).fill({
			type: "align",
			align: "c",
			pregap: .25,
			postgap: .25
		}),
		colSeparationType: "CD",
		hLinesBeforeRow: Array(i.length + 1).fill([])
	};
}
q({
	type: "cdlabel",
	names: ["\\\\cdleft", "\\\\cdright"],
	props: { numArgs: 1 },
	handler(e, t) {
		var { parser: n, funcName: r } = e;
		return {
			type: "cdlabel",
			mode: n.mode,
			side: r.slice(4),
			label: t[0]
		};
	},
	htmlBuilder(e, t) {
		var n = t.havingStyle(t.style.sup()), r = da(Ba(e.label, n, t), t);
		return r.classes.push("cd-label-" + e.side), r.style.bottom = F(.8 - r.depth), r.height = 0, r.depth = 0, r;
	},
	mathmlBuilder(e, t) {
		var n = new J("mrow", [Y(e.label, t)]);
		return n = new J("mpadded", [n]), n.setAttribute("width", "0"), e.side === "left" && n.setAttribute("lspace", "-1width"), n.setAttribute("voffset", "0.7em"), n = new J("mstyle", [n]), n.setAttribute("displaystyle", "false"), n.setAttribute("scriptlevel", "1"), n;
	}
}), q({
	type: "cdlabelparent",
	names: ["\\\\cdparent"],
	props: { numArgs: 1 },
	handler(e, t) {
		var { parser: n } = e;
		return {
			type: "cdlabelparent",
			mode: n.mode,
			fragment: t[0]
		};
	},
	htmlBuilder(e, t) {
		var n = da(Ba(e.fragment, t), t);
		return n.classes.push("cd-vert-arrow"), n;
	},
	mathmlBuilder(e, t) {
		return new J("mrow", [Y(e.fragment, t)]);
	}
}), q({
	type: "textord",
	names: ["\\@char"],
	props: {
		numArgs: 1,
		allowedInText: !0
	},
	handler(e, t) {
		for (var { parser: n } = e, r = Z(t[0], "ordgroup").body, i = "", a = 0; a < r.length; a++) {
			var o = Z(r[a], "textord");
			i += o.text;
		}
		var s = parseInt(i), c;
		if (isNaN(s)) throw new N("\\@char has non-numeric argument " + i);
		if (s < 0 || s >= 1114111) throw new N("\\@char with invalid code point " + i);
		return s <= 65535 ? c = String.fromCharCode(s) : (s -= 65536, c = String.fromCharCode((s >> 10) + 55296, (s & 1023) + 56320)), {
			type: "textord",
			mode: n.mode,
			text: c
		};
	}
});
var Fo = (e, t) => ua(Pa(e.body, t.withColor(e.color), !1)), Io = (e, t) => {
	var n = new J("mstyle", $a(e.body, t.withColor(e.color)));
	return n.setAttribute("mathcolor", e.color), n;
};
q({
	type: "color",
	names: ["\\textcolor"],
	props: {
		numArgs: 2,
		allowedInText: !0,
		argTypes: ["color", "original"]
	},
	handler(e, t) {
		var { parser: n } = e, r = Z(t[0], "color-token").color, i = t[1];
		return {
			type: "color",
			mode: n.mode,
			color: r,
			body: ka(i)
		};
	},
	htmlBuilder: Fo,
	mathmlBuilder: Io
}), q({
	type: "color",
	names: ["\\color"],
	props: {
		numArgs: 1,
		allowedInText: !0,
		argTypes: ["color"]
	},
	handler(e, t) {
		var { parser: n, breakOnTokenText: r } = e, i = Z(t[0], "color-token").color;
		n.gullet.macros.set("\\current@color", i);
		var a = n.parseExpression(!0, r);
		return {
			type: "color",
			mode: n.mode,
			color: i,
			body: a
		};
	},
	htmlBuilder: Fo,
	mathmlBuilder: Io
}), q({
	type: "cr",
	names: ["\\\\"],
	props: {
		numArgs: 0,
		numOptionalArgs: 0,
		allowedInText: !0
	},
	handler(e, t, n) {
		var { parser: r } = e, i = r.gullet.future().text === "[" ? r.parseSizeGroup(!0) : null, a = !r.settings.displayMode || !r.settings.useStrictBehavior("newLineInDisplayMode", "In LaTeX, \\\\ or \\newline does nothing in display mode");
		return {
			type: "cr",
			mode: r.mode,
			newLine: a,
			size: i && Z(i, "size").value
		};
	},
	htmlBuilder(e, t) {
		var n = K(["mspace"], [], t);
		return e.newLine && (n.classes.push("newline"), e.size && (n.style.marginTop = F(Hr(e.size, t)))), n;
	},
	mathmlBuilder(e, t) {
		var n = new J("mspace");
		return e.newLine && (n.setAttribute("linebreak", "newline"), e.size && n.setAttribute("height", F(Hr(e.size, t)))), n;
	}
});
var Lo = {
	"\\global": "\\global",
	"\\long": "\\\\globallong",
	"\\\\globallong": "\\\\globallong",
	"\\def": "\\gdef",
	"\\gdef": "\\gdef",
	"\\edef": "\\xdef",
	"\\xdef": "\\xdef",
	"\\let": "\\\\globallet",
	"\\futurelet": "\\\\globalfuture"
}, Ro = (e) => {
	var t = e.text;
	if (/^(?:[\\{}$&#^_]|EOF)$/.test(t)) throw new N("Expected a control sequence", e);
	return t;
}, zo = (e) => {
	var t = e.gullet.popToken();
	return t.text === "=" && (t = e.gullet.popToken(), t.text === " " && (t = e.gullet.popToken())), t;
}, Bo = (e, t, n, r) => {
	var i = e.gullet.macros.get(n.text);
	i ??= (n.noexpand = !0, {
		tokens: [n],
		numArgs: 0,
		unexpandable: !e.gullet.isExpandable(n.text)
	}), e.gullet.macros.set(t, i, r);
};
q({
	type: "internal",
	names: [
		"\\global",
		"\\long",
		"\\\\globallong"
	],
	props: {
		numArgs: 0,
		allowedInText: !0
	},
	handler(e) {
		var { parser: t, funcName: n } = e;
		t.consumeSpaces();
		var r = t.fetch();
		if (Lo[r.text]) return (n === "\\global" || n === "\\\\globallong") && (r.text = Lo[r.text]), Z(t.parseFunction(), "internal");
		throw new N("Invalid token after macro prefix", r);
	}
}), q({
	type: "internal",
	names: [
		"\\def",
		"\\gdef",
		"\\edef",
		"\\xdef"
	],
	props: {
		numArgs: 0,
		allowedInText: !0,
		primitive: !0
	},
	handler(e) {
		var { parser: t, funcName: n } = e, r = t.gullet.popToken(), i = r.text;
		if (/^(?:[\\{}$&#^_]|EOF)$/.test(i)) throw new N("Expected a control sequence", r);
		for (var a = 0, o, s = [[]]; t.gullet.future().text !== "{";) if (r = t.gullet.popToken(), r.text === "#") {
			if (t.gullet.future().text === "{") {
				o = t.gullet.future(), s[a].push("{");
				break;
			}
			if (r = t.gullet.popToken(), !/^[1-9]$/.test(r.text)) throw new N("Invalid argument number \"" + r.text + "\"");
			if (parseInt(r.text) !== a + 1) throw new N("Argument number \"" + r.text + "\" out of order");
			a++, s.push([]);
		} else if (r.text === "EOF") throw new N("Expected a macro definition");
		else s[a].push(r.text);
		var { tokens: c } = t.gullet.consumeArg();
		return o && c.unshift(o), (n === "\\edef" || n === "\\xdef") && (c = t.gullet.expandTokens(c), c.reverse()), t.gullet.macros.set(i, {
			tokens: c,
			numArgs: a,
			delimiters: s
		}, n === Lo[n]), {
			type: "internal",
			mode: t.mode
		};
	}
}), q({
	type: "internal",
	names: ["\\let", "\\\\globallet"],
	props: {
		numArgs: 0,
		allowedInText: !0,
		primitive: !0
	},
	handler(e) {
		var { parser: t, funcName: n } = e, r = Ro(t.gullet.popToken());
		return t.gullet.consumeSpaces(), Bo(t, r, zo(t), n === "\\\\globallet"), {
			type: "internal",
			mode: t.mode
		};
	}
}), q({
	type: "internal",
	names: ["\\futurelet", "\\\\globalfuture"],
	props: {
		numArgs: 0,
		allowedInText: !0,
		primitive: !0
	},
	handler(e) {
		var { parser: t, funcName: n } = e, r = Ro(t.gullet.popToken()), i = t.gullet.popToken(), a = t.gullet.popToken();
		return Bo(t, r, a, n === "\\\\globalfuture"), t.gullet.pushToken(a), t.gullet.pushToken(i), {
			type: "internal",
			mode: t.mode
		};
	}
});
var Vo = function(e, t, n) {
	var r = ui(pi.math[e] && pi.math[e].replace || e, t, n);
	if (!r) throw Error("Unsupported symbol " + e + " and font size " + t + ".");
	return r;
}, Ho = function(e, t, n, r) {
	var i = n.havingBaseStyle(t), a = K(r.concat(i.sizingClasses(n)), [e], n), o = i.sizeMultiplier / n.sizeMultiplier;
	return a.height *= o, a.depth *= o, a.maxFontSize = i.sizeMultiplier, a;
}, Uo = function(e, t, n) {
	var r = t.havingBaseStyle(n), i = (1 - t.sizeMultiplier / r.sizeMultiplier) * t.fontMetrics().axisHeight;
	e.classes.push("delimcenter"), e.style.top = F(i), e.height -= i, e.depth += i;
}, Wo = function(e, t, n, r, i, a) {
	var o = Ho(ea(e, "Main-Regular", i, r), t, r, a);
	return n && Uo(o, r, t), o;
}, Go = function(e, t, n, r) {
	return ea(e, "Size" + t + "-Regular", n, r);
}, Ko = function(e, t, n, r, i, a) {
	var o = Go(e, t, i, r), s = Ho(K(["delimsizing", "size" + t], [o], r), P.TEXT, r, a);
	return n && Uo(s, r, P.TEXT), s;
}, qo = function(e, t, n) {
	return {
		type: "elem",
		elem: K(["delimsizinginner", t === "Size1-Regular" ? "delim-size1" : "delim-size4"], [K([], [ea(e, t, n)])])
	};
}, Jo = function(e, t, n) {
	var r = oi["Size4-Regular"][e.charCodeAt(0)] ? oi["Size4-Regular"][e.charCodeAt(0)][4] : oi["Size1-Regular"][e.charCodeAt(0)][4], i = sa([], [new ei([new ti("inner", Pr(e, Math.round(1e3 * t)))], {
		width: F(r),
		height: F(t),
		style: "width:" + F(r),
		viewBox: "0 0 " + 1e3 * r + " " + Math.round(1e3 * t),
		preserveAspectRatio: "xMinYMin"
	})], n);
	return i.height = t, i.style.height = F(t), i.style.width = F(r), {
		type: "elem",
		elem: i
	};
}, Yo = .008, Xo = {
	type: "kern",
	size: -1 * Yo
}, Zo = new Set([
	"|",
	"\\lvert",
	"\\rvert",
	"\\vert"
]), Qo = new Set([
	"\\|",
	"\\lVert",
	"\\rVert",
	"\\Vert"
]), $o = function(e, t, n, r, i, a) {
	var o, s, c, l, u = "", d = 0;
	o = c = l = e, s = null;
	var f = "Size1-Regular";
	e === "\\uparrow" ? c = l = "⏐" : e === "\\Uparrow" ? c = l = "‖" : e === "\\downarrow" ? o = c = "⏐" : e === "\\Downarrow" ? o = c = "‖" : e === "\\updownarrow" ? (o = "\\uparrow", c = "⏐", l = "\\downarrow") : e === "\\Updownarrow" ? (o = "\\Uparrow", c = "‖", l = "\\Downarrow") : Zo.has(e) ? (c = "∣", u = "vert", d = 333) : Qo.has(e) ? (c = "∥", u = "doublevert", d = 556) : e === "[" || e === "\\lbrack" ? (o = "⎡", c = "⎢", l = "⎣", f = "Size4-Regular", u = "lbrack", d = 667) : e === "]" || e === "\\rbrack" ? (o = "⎤", c = "⎥", l = "⎦", f = "Size4-Regular", u = "rbrack", d = 667) : e === "\\lfloor" || e === "⌊" ? (c = o = "⎢", l = "⎣", f = "Size4-Regular", u = "lfloor", d = 667) : e === "\\lceil" || e === "⌈" ? (o = "⎡", c = l = "⎢", f = "Size4-Regular", u = "lceil", d = 667) : e === "\\rfloor" || e === "⌋" ? (c = o = "⎥", l = "⎦", f = "Size4-Regular", u = "rfloor", d = 667) : e === "\\rceil" || e === "⌉" ? (o = "⎤", c = l = "⎥", f = "Size4-Regular", u = "rceil", d = 667) : e === "(" || e === "\\lparen" ? (o = "⎛", c = "⎜", l = "⎝", f = "Size4-Regular", u = "lparen", d = 875) : e === ")" || e === "\\rparen" ? (o = "⎞", c = "⎟", l = "⎠", f = "Size4-Regular", u = "rparen", d = 875) : e === "\\{" || e === "\\lbrace" ? (o = "⎧", s = "⎨", l = "⎩", c = "⎪", f = "Size4-Regular") : e === "\\}" || e === "\\rbrace" ? (o = "⎫", s = "⎬", l = "⎭", c = "⎪", f = "Size4-Regular") : e === "\\lgroup" || e === "⟮" ? (o = "⎧", l = "⎩", c = "⎪", f = "Size4-Regular") : e === "\\rgroup" || e === "⟯" ? (o = "⎫", l = "⎭", c = "⎪", f = "Size4-Regular") : e === "\\lmoustache" || e === "⎰" ? (o = "⎧", l = "⎭", c = "⎪", f = "Size4-Regular") : (e === "\\rmoustache" || e === "⎱") && (o = "⎫", l = "⎩", c = "⎪", f = "Size4-Regular");
	var p = Vo(o, f, i), m = p.height + p.depth, h = Vo(c, f, i), g = h.height + h.depth, _ = Vo(l, f, i), v = _.height + _.depth, y = 0, b = 1;
	if (s !== null) {
		var x = Vo(s, f, i);
		y = x.height + x.depth, b = 2;
	}
	var S = m + v + y, C = S + Math.max(0, Math.ceil((t - S) / (b * g))) * b * g, w = r.fontMetrics().axisHeight;
	n && (w *= r.sizeMultiplier);
	var T = C / 2 - w, E = [];
	if (u.length > 0) {
		var ee = C - m - v, D = Math.round(C * 1e3), te = Ir(u, Math.round(ee * 1e3)), ne = new ti(u, te), re = F(d / 1e3), ie = F(D / 1e3), ae = sa([], [new ei([ne], {
			width: re,
			height: ie,
			viewBox: "0 0 " + d + " " + D
		})], r);
		ae.height = D / 1e3, ae.style.width = re, ae.style.height = ie, E.push({
			type: "elem",
			elem: ae
		});
	} else {
		if (E.push(qo(l, f, i)), E.push(Xo), s === null) {
			var oe = C - m - v + 2 * Yo;
			E.push(Jo(c, oe, r));
		} else {
			var se = (C - m - v - y) / 2 + 2 * Yo;
			E.push(Jo(c, se, r)), E.push(Xo), E.push(qo(s, f, i)), E.push(Xo), E.push(Jo(c, se, r));
		}
		E.push(Xo), E.push(qo(o, f, i));
	}
	var O = r.havingBaseStyle(P.TEXT);
	return Ho(K(["delimsizing", "mult"], [pa({
		positionType: "bottom",
		positionData: T,
		children: E
	})], O), P.TEXT, r, a);
}, es = 80, ts = .08, ns = function(e, t, n, r, i) {
	return sa(["hide-tail"], [new ei([new ti(e, Nr(e, r, n))], {
		width: "400em",
		height: F(t),
		viewBox: "0 0 400000 " + n,
		preserveAspectRatio: "xMinYMin slice"
	})], i);
}, rs = function(e, t) {
	var n = t.havingBaseSizing(), r = ps("\\surd", e * n.sizeMultiplier, ds, n), i = n.sizeMultiplier, a = Math.max(0, t.minRuleThickness - t.fontMetrics().sqrtRuleThickness), o, s, c, l, u;
	return r.type === "small" ? (l = 1e3 + 1e3 * a + es, e < 1 ? i = 1 : e < 1.4 && (i = .7), s = (1 + a + ts) / i, c = (1 + a) / i, o = ns("sqrtMain", s, l, a, t), o.style.minWidth = "0.853em", u = .833 / i) : r.type === "large" ? (l = (1e3 + es) * ss[r.size], c = (ss[r.size] + a) / i, s = (ss[r.size] + a + ts) / i, o = ns("sqrtSize" + r.size, s, l, a, t), o.style.minWidth = "1.02em", u = 1 / i) : (s = e + a + ts, c = e + a, l = Math.floor(1e3 * e + a) + es, o = ns("sqrtTall", s, l, a, t), o.style.minWidth = "0.742em", u = 1.056), o.height = c, o.style.height = F(s), {
		span: o,
		advanceWidth: u,
		ruleWidth: (t.fontMetrics().sqrtRuleThickness + a) * i
	};
}, is = new Set([
	"(",
	"\\lparen",
	")",
	"\\rparen",
	"[",
	"\\lbrack",
	"]",
	"\\rbrack",
	"\\{",
	"\\lbrace",
	"\\}",
	"\\rbrace",
	"\\lfloor",
	"\\rfloor",
	"⌊",
	"⌋",
	"\\lceil",
	"\\rceil",
	"⌈",
	"⌉",
	"\\surd"
]), as = new Set([
	"\\uparrow",
	"\\downarrow",
	"\\updownarrow",
	"\\Uparrow",
	"\\Downarrow",
	"\\Updownarrow",
	"|",
	"\\|",
	"\\vert",
	"\\Vert",
	"\\lvert",
	"\\rvert",
	"\\lVert",
	"\\rVert",
	"\\lgroup",
	"\\rgroup",
	"⟮",
	"⟯",
	"\\lmoustache",
	"\\rmoustache",
	"⎰",
	"⎱"
]), os = new Set([
	"<",
	">",
	"\\langle",
	"\\rangle",
	"/",
	"\\backslash",
	"\\lt",
	"\\gt"
]), ss = [
	0,
	1.2,
	1.8,
	2.4,
	3
], cs = function(e, t, n, r, i) {
	if (e === "<" || e === "\\lt" || e === "⟨" ? e = "\\langle" : (e === ">" || e === "\\gt" || e === "⟩") && (e = "\\rangle"), is.has(e) || os.has(e)) return Ko(e, t, !1, n, r, i);
	if (as.has(e)) return $o(e, ss[t], !1, n, r, i);
	throw new N("Illegal delimiter: '" + e + "'");
}, ls = [
	{
		type: "small",
		style: P.SCRIPTSCRIPT
	},
	{
		type: "small",
		style: P.SCRIPT
	},
	{
		type: "small",
		style: P.TEXT
	},
	{
		type: "large",
		size: 1
	},
	{
		type: "large",
		size: 2
	},
	{
		type: "large",
		size: 3
	},
	{
		type: "large",
		size: 4
	}
], us = [
	{
		type: "small",
		style: P.SCRIPTSCRIPT
	},
	{
		type: "small",
		style: P.SCRIPT
	},
	{
		type: "small",
		style: P.TEXT
	},
	{ type: "stack" }
], ds = [
	{
		type: "small",
		style: P.SCRIPTSCRIPT
	},
	{
		type: "small",
		style: P.SCRIPT
	},
	{
		type: "small",
		style: P.TEXT
	},
	{
		type: "large",
		size: 1
	},
	{
		type: "large",
		size: 2
	},
	{
		type: "large",
		size: 3
	},
	{
		type: "large",
		size: 4
	},
	{ type: "stack" }
], fs = function(e) {
	if (e.type === "small") return "Main-Regular";
	if (e.type === "large") return "Size" + e.size + "-Regular";
	if (e.type === "stack") return "Size4-Regular";
	var t = e.type;
	throw Error("Add support for delim type '" + t + "' here.");
}, ps = function(e, t, n, r) {
	for (var i = Math.min(2, 3 - r.style.size); i < n.length; i++) {
		var a = n[i];
		if (a.type === "stack") break;
		var o = Vo(e, fs(a), "math"), s = o.height + o.depth;
		if (a.type === "small") {
			var c = r.havingBaseStyle(a.style);
			s *= c.sizeMultiplier;
		}
		if (s > t) return a;
	}
	return n[n.length - 1];
}, ms = function(e, t, n, r, i, a) {
	e === "<" || e === "\\lt" || e === "⟨" ? e = "\\langle" : (e === ">" || e === "\\gt" || e === "⟩") && (e = "\\rangle");
	var o = os.has(e) ? ls : is.has(e) ? ds : us, s = ps(e, t, o, r);
	return s.type === "small" ? Wo(e, s.style, n, r, i, a) : s.type === "large" ? Ko(e, s.size, n, r, i, a) : $o(e, t, n, r, i, a);
}, hs = function(e, t, n, r, i, a) {
	var o = r.fontMetrics().axisHeight * r.sizeMultiplier, s = 901, c = 5 / r.fontMetrics().ptPerEm, l = Math.max(t - o, n + o);
	return ms(e, Math.max(l / 500 * s, 2 * l - c), !0, r, i, a);
}, gs = {
	"\\bigl": {
		mclass: "mopen",
		size: 1
	},
	"\\Bigl": {
		mclass: "mopen",
		size: 2
	},
	"\\biggl": {
		mclass: "mopen",
		size: 3
	},
	"\\Biggl": {
		mclass: "mopen",
		size: 4
	},
	"\\bigr": {
		mclass: "mclose",
		size: 1
	},
	"\\Bigr": {
		mclass: "mclose",
		size: 2
	},
	"\\biggr": {
		mclass: "mclose",
		size: 3
	},
	"\\Biggr": {
		mclass: "mclose",
		size: 4
	},
	"\\bigm": {
		mclass: "mrel",
		size: 1
	},
	"\\Bigm": {
		mclass: "mrel",
		size: 2
	},
	"\\biggm": {
		mclass: "mrel",
		size: 3
	},
	"\\Biggm": {
		mclass: "mrel",
		size: 4
	},
	"\\big": {
		mclass: "mord",
		size: 1
	},
	"\\Big": {
		mclass: "mord",
		size: 2
	},
	"\\bigg": {
		mclass: "mord",
		size: 3
	},
	"\\Bigg": {
		mclass: "mord",
		size: 4
	}
}, _s = new Set(/* @__PURE__ */ "(,\\lparen,),\\rparen,[,\\lbrack,],\\rbrack,\\{,\\lbrace,\\},\\rbrace,\\lfloor,\\rfloor,⌊,⌋,\\lceil,\\rceil,⌈,⌉,<,>,\\langle,⟨,\\rangle,⟩,\\lt,\\gt,\\lvert,\\rvert,\\lVert,\\rVert,\\lgroup,\\rgroup,⟮,⟯,\\lmoustache,\\rmoustache,⎰,⎱,/,\\backslash,|,\\vert,\\|,\\Vert,\\uparrow,\\Uparrow,\\downarrow,\\Downarrow,\\updownarrow,\\Updownarrow,.".split(","));
function vs(e) {
	return "isMiddle" in e;
}
function ys(e, t) {
	var n = bo(e);
	if (n && _s.has(n.text)) return n;
	throw n ? new N("Invalid delimiter '" + n.text + "' after '" + t.funcName + "'", e) : new N("Invalid delimiter type '" + e.type + "'", e);
}
q({
	type: "delimsizing",
	names: [
		"\\bigl",
		"\\Bigl",
		"\\biggl",
		"\\Biggl",
		"\\bigr",
		"\\Bigr",
		"\\biggr",
		"\\Biggr",
		"\\bigm",
		"\\Bigm",
		"\\biggm",
		"\\Biggm",
		"\\big",
		"\\Big",
		"\\bigg",
		"\\Bigg"
	],
	props: {
		numArgs: 1,
		argTypes: ["primitive"]
	},
	handler: (e, t) => {
		var n = ys(t[0], e);
		return {
			type: "delimsizing",
			mode: e.parser.mode,
			size: gs[e.funcName].size,
			mclass: gs[e.funcName].mclass,
			delim: n.text
		};
	},
	htmlBuilder: (e, t) => e.delim === "." ? K([e.mclass]) : cs(e.delim, e.size, t, e.mode, [e.mclass]),
	mathmlBuilder: (e) => {
		var t = [];
		e.delim !== "." && t.push(Ja(e.delim, e.mode));
		var n = new J("mo", t);
		e.mclass === "mopen" || e.mclass === "mclose" ? n.setAttribute("fence", "true") : n.setAttribute("fence", "false"), n.setAttribute("stretchy", "true");
		var r = F(ss[e.size]);
		return n.setAttribute("minsize", r), n.setAttribute("maxsize", r), n;
	}
});
function bs(e) {
	if (!e.body) throw Error("Bug: The leftright ParseNode wasn't fully parsed.");
}
q({
	type: "leftright-right",
	names: ["\\right"],
	props: {
		numArgs: 1,
		primitive: !0
	},
	handler: (e, t) => {
		var n = e.parser.gullet.macros.get("\\current@color");
		if (n && typeof n != "string") throw new N("\\current@color set to non-string in \\right");
		return {
			type: "leftright-right",
			mode: e.parser.mode,
			delim: ys(t[0], e).text,
			color: n
		};
	}
}), q({
	type: "leftright",
	names: ["\\left"],
	props: {
		numArgs: 1,
		primitive: !0
	},
	handler: (e, t) => {
		var n = ys(t[0], e), r = e.parser;
		++r.leftrightDepth;
		var i = r.parseExpression(!1);
		--r.leftrightDepth, r.expect("\\right", !1);
		var a = Z(r.parseFunction(), "leftright-right");
		return {
			type: "leftright",
			mode: r.mode,
			body: i,
			left: n.text,
			right: a.delim,
			rightColor: a.color
		};
	},
	htmlBuilder: (e, t) => {
		bs(e);
		for (var n = Pa(e.body, t, !0, ["mopen", "mclose"]), r = 0, i = 0, a = !1, o = 0; o < n.length; o++) {
			var s = n[o];
			vs(s) ? a = !0 : (r = Math.max(n[o].height, r), i = Math.max(n[o].depth, i));
		}
		r *= t.sizeMultiplier, i *= t.sizeMultiplier;
		var c = e.left === "." ? za(t, ["mopen"]) : hs(e.left, r, i, t, e.mode, ["mopen"]);
		if (n.unshift(c), a) for (var l = 1; l < n.length; l++) {
			var u = n[l];
			if (vs(u)) {
				var d = u.isMiddle;
				n[l] = hs(d.delim, r, i, d.options, e.mode, []);
			}
		}
		var f;
		if (e.right === ".") f = za(t, ["mclose"]);
		else {
			var p = e.rightColor ? t.withColor(e.rightColor) : t;
			f = hs(e.right, r, i, p, e.mode, ["mclose"]);
		}
		return n.push(f), K(["minner"], n, t);
	},
	mathmlBuilder: (e, t) => {
		bs(e);
		var n = $a(e.body, t);
		if (e.left !== ".") {
			var r = new J("mo", [Ja(e.left, e.mode)]);
			r.setAttribute("fence", "true"), n.unshift(r);
		}
		if (e.right !== ".") {
			var i = new J("mo", [Ja(e.right, e.mode)]);
			i.setAttribute("fence", "true"), e.rightColor && i.setAttribute("mathcolor", e.rightColor), n.push(i);
		}
		return Ya(n);
	}
}), q({
	type: "middle",
	names: ["\\middle"],
	props: {
		numArgs: 1,
		primitive: !0
	},
	handler: (e, t) => {
		var n = ys(t[0], e);
		if (!e.parser.leftrightDepth) throw new N("\\middle without preceding \\left", n);
		return {
			type: "middle",
			mode: e.parser.mode,
			delim: n.text
		};
	},
	htmlBuilder: (e, t) => {
		var n;
		return e.delim === "." ? n = za(t, []) : (n = cs(e.delim, 1, t, e.mode, []), n.isMiddle = {
			delim: e.delim,
			options: t
		}), n;
	},
	mathmlBuilder: (e, t) => {
		var n = new J("mo", [e.delim === "\\vert" || e.delim === "|" ? Ja("|", "text") : Ja(e.delim, e.mode)]);
		return n.setAttribute("fence", "true"), n.setAttribute("lspace", "0.05em"), n.setAttribute("rspace", "0.05em"), n;
	}
});
var xs = (e, t) => {
	var n = da(Ba(e.body, t), t), r = e.label.slice(1), i = t.sizeMultiplier, a, o, s = Zn(e.body);
	if (r === "sout") a = K(["stretchy", "sout"]), a.height = t.fontMetrics().defaultRuleThickness / i, o = -.5 * t.fontMetrics().xHeight;
	else if (r === "phase") {
		var c = Hr({
			number: .6,
			unit: "pt"
		}, t), l = Hr({
			number: .35,
			unit: "ex"
		}, t), u = t.havingBaseSizing();
		i /= u.sizeMultiplier;
		var d = n.height + n.depth + c + l;
		n.style.paddingLeft = F(d / 2 + c);
		var f = Math.floor(1e3 * d * i);
		a = sa(["hide-tail"], [new ei([new ti("phase", jr(f))], {
			width: "400em",
			height: F(f / 1e3),
			viewBox: "0 0 400000 " + f,
			preserveAspectRatio: "xMinYMin slice"
		})], t), a.style.height = F(d), o = n.depth + c + l;
	} else {
		/cancel/.test(r) ? s || n.classes.push("cancel-pad") : r === "angl" ? n.classes.push("anglpad") : n.classes.push("boxpad");
		var p, m, h = 0;
		/box/.test(r) ? (h = Math.max(t.fontMetrics().fboxrule, t.minRuleThickness), p = t.fontMetrics().fboxsep + (r === "colorbox" ? 0 : h), m = p) : r === "angl" ? (h = Math.max(t.fontMetrics().defaultRuleThickness, t.minRuleThickness), p = 4 * h, m = Math.max(0, .25 - n.depth)) : (p = s ? .2 : 0, m = p), a = ho(n, r, p, m, t), /fbox|boxed|fcolorbox/.test(r) ? (a.style.borderStyle = "solid", a.style.borderWidth = F(h)) : r === "angl" && h !== .049 && (a.style.borderTopWidth = F(h), a.style.borderRightWidth = F(h)), o = n.depth + m, e.backgroundColor && (a.style.backgroundColor = e.backgroundColor, e.borderColor && (a.style.borderColor = e.borderColor));
	}
	var g;
	if (e.backgroundColor) g = pa({
		positionType: "individualShift",
		children: [{
			type: "elem",
			elem: a,
			shift: o
		}, {
			type: "elem",
			elem: n,
			shift: 0
		}]
	});
	else {
		var _ = /cancel|phase/.test(r) ? ["svg-align"] : [];
		g = pa({
			positionType: "individualShift",
			children: [{
				type: "elem",
				elem: n,
				shift: 0
			}, {
				type: "elem",
				elem: a,
				shift: o,
				wrapperClasses: _
			}]
		});
	}
	return /cancel/.test(r) && (g.height = n.height, g.depth = n.depth), /cancel/.test(r) && !s ? K(["mord", "cancel-lap"], [g], t) : K(["mord"], [g], t);
}, Ss = (e, t) => {
	var n, r = new J(e.label.includes("colorbox") ? "mpadded" : "menclose", [Y(e.body, t)]);
	switch (e.label) {
		case "\\cancel":
			r.setAttribute("notation", "updiagonalstrike");
			break;
		case "\\bcancel":
			r.setAttribute("notation", "downdiagonalstrike");
			break;
		case "\\phase":
			r.setAttribute("notation", "phasorangle");
			break;
		case "\\sout":
			r.setAttribute("notation", "horizontalstrike");
			break;
		case "\\fbox":
			r.setAttribute("notation", "box");
			break;
		case "\\angl":
			r.setAttribute("notation", "actuarial");
			break;
		case "\\fcolorbox":
		case "\\colorbox":
			if (n = t.fontMetrics().fboxsep * t.fontMetrics().ptPerEm, r.setAttribute("width", "+" + 2 * n + "pt"), r.setAttribute("height", "+" + 2 * n + "pt"), r.setAttribute("lspace", n + "pt"), r.setAttribute("voffset", n + "pt"), e.label === "\\fcolorbox") {
				var i = Math.max(t.fontMetrics().fboxrule, t.minRuleThickness);
				r.setAttribute("style", "border: " + F(i) + " solid " + e.borderColor);
			}
			break;
		case "\\xcancel":
			r.setAttribute("notation", "updiagonalstrike downdiagonalstrike");
			break;
	}
	return e.backgroundColor && r.setAttribute("mathbackground", e.backgroundColor), r;
};
q({
	type: "enclose",
	names: ["\\colorbox"],
	props: {
		numArgs: 2,
		allowedInText: !0,
		argTypes: ["color", "hbox"]
	},
	handler(e, t, n) {
		var { parser: r, funcName: i } = e, a = Z(t[0], "color-token").color, o = t[1];
		return {
			type: "enclose",
			mode: r.mode,
			label: i,
			backgroundColor: a,
			body: o
		};
	},
	htmlBuilder: xs,
	mathmlBuilder: Ss
}), q({
	type: "enclose",
	names: ["\\fcolorbox"],
	props: {
		numArgs: 3,
		allowedInText: !0,
		argTypes: [
			"color",
			"color",
			"hbox"
		]
	},
	handler(e, t, n) {
		var { parser: r, funcName: i } = e, a = Z(t[0], "color-token").color, o = Z(t[1], "color-token").color, s = t[2];
		return {
			type: "enclose",
			mode: r.mode,
			label: i,
			backgroundColor: o,
			borderColor: a,
			body: s
		};
	},
	htmlBuilder: xs,
	mathmlBuilder: Ss
}), q({
	type: "enclose",
	names: ["\\fbox"],
	props: {
		numArgs: 1,
		argTypes: ["hbox"],
		allowedInText: !0
	},
	handler(e, t) {
		var { parser: n } = e;
		return {
			type: "enclose",
			mode: n.mode,
			label: "\\fbox",
			body: t[0]
		};
	}
}), q({
	type: "enclose",
	names: [
		"\\cancel",
		"\\bcancel",
		"\\xcancel",
		"\\phase"
	],
	props: { numArgs: 1 },
	handler(e, t) {
		var { parser: n, funcName: r } = e, i = t[0];
		return {
			type: "enclose",
			mode: n.mode,
			label: r,
			body: i
		};
	},
	htmlBuilder: xs,
	mathmlBuilder: Ss
}), q({
	type: "enclose",
	names: ["\\sout"],
	props: {
		numArgs: 1,
		allowedInText: !0
	},
	handler(e, t) {
		var { parser: n, funcName: r } = e;
		n.mode === "math" && n.settings.reportNonstrict("mathVsSout", "LaTeX's \\sout works only in text mode");
		var i = t[0];
		return {
			type: "enclose",
			mode: n.mode,
			label: r,
			body: i
		};
	},
	htmlBuilder: xs,
	mathmlBuilder: Ss
}), q({
	type: "enclose",
	names: ["\\angl"],
	props: {
		numArgs: 1,
		argTypes: ["hbox"],
		allowedInText: !1
	},
	handler(e, t) {
		var { parser: n } = e;
		return {
			type: "enclose",
			mode: n.mode,
			label: "\\angl",
			body: t[0]
		};
	}
});
var Cs = {};
function ws(e) {
	for (var { type: t, names: n, props: r, handler: i, htmlBuilder: a, mathmlBuilder: o } = e, s = {
		type: t,
		numArgs: r.numArgs || 0,
		allowedInText: !1,
		numOptionalArgs: 0,
		handler: i
	}, c = 0; c < n.length; ++c) Cs[n[c]] = s;
	a && (Ta[t] = a), o && (Ea[t] = o);
}
var Ts = {};
function Q(e, t) {
	Ts[e] = t;
}
var Es = class e {
	constructor(e, t, n) {
		this.lexer = void 0, this.start = void 0, this.end = void 0, this.lexer = e, this.start = t, this.end = n;
	}
	static range(t, n) {
		return n ? !t || !t.loc || !n.loc || t.loc.lexer !== n.loc.lexer ? null : new e(t.loc.lexer, t.loc.start, n.loc.end) : t && t.loc;
	}
}, Ds = class e {
	constructor(e, t) {
		this.text = void 0, this.loc = void 0, this.noexpand = void 0, this.treatAsRelax = void 0, this.text = e, this.loc = t;
	}
	range(t, n) {
		return new e(n, Es.range(this, t));
	}
};
function Os(e) {
	var t = [];
	e.consumeSpaces();
	var n = e.fetch().text;
	for (n === "\\relax" && (e.consume(), e.consumeSpaces(), n = e.fetch().text); n === "\\hline" || n === "\\hdashline";) e.consume(), t.push(n === "\\hdashline"), e.consumeSpaces(), n = e.fetch().text;
	return t;
}
var ks = (e) => {
	if (!e.parser.settings.displayMode) throw new N("{" + e.envName + "} can be used only in display mode.");
}, As = new Set(["gather", "gather*"]);
function js(e) {
	if (!e.includes("ed")) return !e.includes("*");
}
function Ms(e, t, n) {
	var { hskipBeforeAndAfter: r, addJot: i, cols: a, arraystretch: o, colSeparationType: s, autoTag: c, singleRow: l, emptySingleRow: u, maxNumCols: d, leqno: f } = t;
	if (e.gullet.beginGroup(), l || e.gullet.macros.set("\\cr", "\\\\\\relax"), !o) {
		var p = e.gullet.expandMacroAsText("\\arraystretch");
		if (p == null) o = 1;
		else if (o = parseFloat(p), !o || o < 0) throw new N("Invalid \\arraystretch: " + p);
	}
	e.gullet.beginGroup();
	var m = [], h = [m], g = [], _ = [], v = c == null ? void 0 : [];
	function y() {
		c && e.gullet.macros.set("\\@eqnsw", "1", !0);
	}
	function b() {
		v && (e.gullet.macros.get("\\df@tag") ? (v.push(e.subparse([new Ds("\\df@tag")])), e.gullet.macros.set("\\df@tag", void 0, !0)) : v.push(!!c && e.gullet.macros.get("\\@eqnsw") === "1"));
	}
	for (y(), _.push(Os(e));;) {
		var x = e.parseExpression(!1, l ? "\\end" : "\\\\");
		e.gullet.endGroup(), e.gullet.beginGroup();
		var S = {
			type: "ordgroup",
			mode: e.mode,
			body: x
		};
		n && (S = {
			type: "styling",
			mode: e.mode,
			style: n,
			resetFont: !0,
			body: [S]
		}), m.push(S);
		var C = e.fetch().text;
		if (C === "&") {
			if (d && m.length === d) {
				if (l || s) throw new N("Too many tab characters: &", e.nextToken);
				e.settings.reportNonstrict("textEnv", "Too few columns specified in the {array} column argument.");
			}
			e.consume();
		} else if (C === "\\end") {
			b(), m.length === 1 && S.type === "styling" && S.body.length === 1 && S.body[0].type === "ordgroup" && S.body[0].body.length === 0 && (h.length > 1 || !u) && h.pop(), _.length < h.length + 1 && _.push([]);
			break;
		} else if (C === "\\\\") {
			e.consume();
			var w = void 0;
			e.gullet.future().text !== " " && (w = e.parseSizeGroup(!0)), g.push(w ? w.value : null), b(), _.push(Os(e)), m = [], h.push(m), y();
		} else throw new N("Expected & or \\\\ or \\cr or \\end", e.nextToken);
	}
	return e.gullet.endGroup(), e.gullet.endGroup(), {
		type: "array",
		mode: e.mode,
		addJot: i,
		arraystretch: o,
		body: h,
		cols: a,
		rowGaps: g,
		hskipBeforeAndAfter: r,
		hLinesBeforeRow: _,
		colSeparationType: s,
		tags: v,
		leqno: f
	};
}
function Ns(e) {
	return e.slice(0, 1) === "d" ? "display" : "text";
}
var Ps = function(e, t) {
	var n, r, i = e.body.length, a = e.hLinesBeforeRow, o = 0, s = Array(i), c = [], l = Math.max(t.fontMetrics().arrayRuleWidth, t.minRuleThickness), u = 1 / t.fontMetrics().ptPerEm, d = 5 * u;
	e.colSeparationType && e.colSeparationType === "small" && (d = .2778 * (t.havingStyle(P.SCRIPT).sizeMultiplier / t.sizeMultiplier));
	var f = e.colSeparationType === "CD" ? Hr({
		number: 3,
		unit: "ex"
	}, t) : 12 * u, p = 3 * u, m = e.arraystretch * f, h = .7 * m, g = .3 * m, _ = 0;
	function v(e) {
		for (var t = 0; t < e.length; ++t) t > 0 && (_ += .25), c.push({
			pos: _,
			isDashed: e[t]
		});
	}
	for (v(a[0]), n = 0; n < e.body.length; ++n) {
		var y = e.body[n], b = h, x = g;
		o < y.length && (o = y.length);
		var S = {
			cells: Array(y.length),
			height: 0,
			depth: 0,
			pos: 0
		};
		for (r = 0; r < y.length; ++r) {
			var C = Ba(y[r], t);
			x < C.depth && (x = C.depth), b < C.height && (b = C.height), S.cells[r] = C;
		}
		var w = e.rowGaps[n], T = 0;
		w && (T = Hr(w, t), T > 0 && (T += g, x < T && (x = T), T = 0)), e.addJot && n < e.body.length - 1 && (x += p), S.height = b, S.depth = x, _ += b, S.pos = _, _ += x + T, s[n] = S, v(a[n + 1]);
	}
	var E = _ / 2 + t.fontMetrics().axisHeight, ee = e.cols || [], D = [], te, ne, re = [];
	if (e.tags && e.tags.some((e) => e)) for (n = 0; n < i; ++n) {
		var ie = s[n], ae = ie.pos - E, oe = e.tags[n], se = void 0;
		se = oe === !0 ? K(["eqn-num"], [], t) : oe === !1 ? K([], [], t) : K([], Pa(oe, t, !0), t), se.depth = ie.depth, se.height = ie.height, re.push({
			type: "elem",
			elem: se,
			shift: ae
		});
	}
	for (r = 0, ne = 0; r < o || ne < ee.length; ++r, ++ne) {
		for (var O = ee[ne], k = !0; (ce = O)?.type === "separator";) {
			var ce;
			if (k || (te = K(["arraycolsep"], []), te.style.width = F(t.fontMetrics().doubleRuleSep), D.push(te)), O.separator === "|" || O.separator === ":") {
				var le = O.separator === "|" ? "solid" : "dashed", ue = K(["vertical-separator"], [], t);
				ue.style.height = F(_), ue.style.borderRightWidth = F(l), ue.style.borderRightStyle = le, ue.style.margin = "0 " + F(-l / 2);
				var de = _ - E;
				de && (ue.style.verticalAlign = F(-de)), D.push(ue);
			} else throw new N("Invalid separator type: " + O.separator);
			ne++, O = ee[ne], k = !1;
		}
		if (!(r >= o)) {
			var fe = void 0;
			(r > 0 || e.hskipBeforeAndAfter) && (fe = O?.pregap ?? d, fe !== 0 && (te = K(["arraycolsep"], []), te.style.width = F(fe), D.push(te)));
			var pe = [];
			for (n = 0; n < i; ++n) {
				var me = s[n], he = me.cells[r];
				if (he) {
					var ge = me.pos - E;
					he.depth = me.depth, he.height = me.height, pe.push({
						type: "elem",
						elem: he,
						shift: ge
					});
				}
			}
			var _e = pa({
				positionType: "individualShift",
				children: pe
			}), ve = K(["col-align-" + (O?.align || "c")], [_e]);
			D.push(ve), (r < o - 1 || e.hskipBeforeAndAfter) && (fe = O?.postgap ?? d, fe !== 0 && (te = K(["arraycolsep"], []), te.style.width = F(fe), D.push(te)));
		}
	}
	var ye = K(["mtable"], D);
	if (c.length > 0) {
		for (var be = ca("hline", t, l), xe = ca("hdashline", t, l), Se = [{
			type: "elem",
			elem: ye,
			shift: 0
		}]; c.length > 0;) {
			var Ce = c.pop(), we = Ce.pos - E;
			Ce.isDashed ? Se.push({
				type: "elem",
				elem: xe,
				shift: we
			}) : Se.push({
				type: "elem",
				elem: be,
				shift: we
			});
		}
		ye = pa({
			positionType: "individualShift",
			children: Se
		});
	}
	if (re.length === 0) return K(["mord"], [ye], t);
	var Te = K(["tag"], [pa({
		positionType: "individualShift",
		children: re
	})], t);
	return ua([ye, Te]);
}, Fs = {
	c: "center ",
	l: "left ",
	r: "right "
}, Is = function(e, t) {
	for (var n = [], r = new J("mtd", [], ["mtr-glue"]), i = new J("mtd", [], ["mml-eqn-num"]), a = 0; a < e.body.length; a++) {
		for (var o = e.body[a], s = [], c = 0; c < o.length; c++) s.push(new J("mtd", [Y(o[c], t)]));
		e.tags && e.tags[a] && (s.unshift(r), s.push(r), e.leqno ? s.unshift(i) : s.push(i)), n.push(new J("mtr", s));
	}
	var l = new J("mtable", n), u = e.arraystretch === .5 ? .1 : .16 + e.arraystretch - 1 + (e.addJot ? .09 : 0);
	l.setAttribute("rowspacing", F(u));
	var d = "", f = "";
	if (e.cols && e.cols.length > 0) {
		var p = e.cols, m = "", h = !1, g = 0, _ = p.length;
		p[0].type === "separator" && (d += "top ", g = 1), p[p.length - 1].type === "separator" && (d += "bottom ", --_);
		for (var v = g; v < _; v++) {
			var y = p[v];
			y.type === "align" ? (f += Fs[y.align], h && (m += "none "), h = !0) : y.type === "separator" && (h &&= (m += y.separator === "|" ? "solid " : "dashed ", !1));
		}
		l.setAttribute("columnalign", f.trim()), /[sd]/.test(m) && l.setAttribute("columnlines", m.trim());
	}
	if (e.colSeparationType === "align") {
		for (var b = e.cols || [], x = "", S = 1; S < b.length; S++) x += S % 2 ? "0em " : "1em ";
		l.setAttribute("columnspacing", x.trim());
	} else e.colSeparationType === "alignat" || e.colSeparationType === "gather" ? l.setAttribute("columnspacing", "0em") : e.colSeparationType === "small" ? l.setAttribute("columnspacing", "0.2778em") : e.colSeparationType === "CD" ? l.setAttribute("columnspacing", "0.5em") : l.setAttribute("columnspacing", "1em");
	var C = "", w = e.hLinesBeforeRow;
	d += w[0].length > 0 ? "left " : "", d += w[w.length - 1].length > 0 ? "right " : "";
	for (var T = 1; T < w.length - 1; T++) C += w[T].length === 0 ? "none " : w[T][0] ? "dashed " : "solid ";
	return /[sd]/.test(C) && l.setAttribute("rowlines", C.trim()), d !== "" && (l = new J("menclose", [l]), l.setAttribute("notation", d.trim())), e.arraystretch && e.arraystretch < 1 && (l = new J("mstyle", [l]), l.setAttribute("scriptlevel", "1")), l;
}, Ls = function(e, t) {
	e.envName.includes("ed") || ks(e);
	var n = [], r = e.envName.includes("at") ? "alignat" : "align", i = e.envName === "split", a = Ms(e.parser, {
		cols: n,
		addJot: !0,
		autoTag: i ? void 0 : js(e.envName),
		emptySingleRow: !0,
		colSeparationType: r,
		maxNumCols: i ? 2 : void 0,
		leqno: e.parser.settings.leqno
	}, "display"), o = 0, s = 0, c = {
		type: "ordgroup",
		mode: e.mode,
		body: []
	};
	if (t[0] && t[0].type === "ordgroup") {
		for (var l = "", u = 0; u < t[0].body.length; u++) {
			var d = Z(t[0].body[u], "textord");
			l += d.text;
		}
		o = Number(l), s = o * 2;
	}
	var f = !s;
	a.body.forEach(function(e) {
		for (var t = 1; t < e.length; t += 2) Z(Z(e[t], "styling").body[0], "ordgroup").body.unshift(c);
		if (f) s < e.length && (s = e.length);
		else {
			var n = e.length / 2;
			if (o < n) throw new N("Too many math in a row: " + ("expected " + o + ", but got " + n), e[0]);
		}
	});
	for (var p = 0; p < s; ++p) {
		var m = "r", h = 0;
		p % 2 == 1 ? m = "l" : p > 0 && f && (h = 1), n[p] = {
			type: "align",
			align: m,
			pregap: h,
			postgap: 0
		};
	}
	return a.colSeparationType = f ? "align" : "alignat", a;
};
ws({
	type: "array",
	names: ["array", "darray"],
	props: { numArgs: 1 },
	handler(e, t) {
		var n = (bo(t[0]) ? [t[0]] : Z(t[0], "ordgroup").body).map(function(e) {
			var t = yo(e).text;
			if ("lcr".includes(t)) return {
				type: "align",
				align: t
			};
			if (t === "|") return {
				type: "separator",
				separator: "|"
			};
			if (t === ":") return {
				type: "separator",
				separator: ":"
			};
			throw new N("Unknown column alignment: " + t, e);
		}), r = {
			cols: n,
			hskipBeforeAndAfter: !0,
			maxNumCols: n.length
		};
		return Ms(e.parser, r, Ns(e.envName));
	},
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: [
		"matrix",
		"pmatrix",
		"bmatrix",
		"Bmatrix",
		"vmatrix",
		"Vmatrix",
		"matrix*",
		"pmatrix*",
		"bmatrix*",
		"Bmatrix*",
		"vmatrix*",
		"Vmatrix*"
	],
	props: { numArgs: 0 },
	handler(e) {
		var t = {
			matrix: null,
			pmatrix: ["(", ")"],
			bmatrix: ["[", "]"],
			Bmatrix: ["\\{", "\\}"],
			vmatrix: ["|", "|"],
			Vmatrix: ["\\Vert", "\\Vert"]
		}[e.envName.replace("*", "")], n = "c", r = {
			hskipBeforeAndAfter: !1,
			cols: [{
				type: "align",
				align: n
			}]
		};
		if (e.envName.charAt(e.envName.length - 1) === "*") {
			var i = e.parser;
			if (i.consumeSpaces(), i.fetch().text === "[") {
				if (i.consume(), i.consumeSpaces(), n = i.fetch().text, !"lcr".includes(n)) throw new N("Expected l or c or r", i.nextToken);
				i.consume(), i.consumeSpaces(), i.expect("]"), i.consume(), r.cols = [{
					type: "align",
					align: n
				}];
			}
		}
		var a = Ms(e.parser, r, Ns(e.envName)), o = Math.max(0, ...a.body.map((e) => e.length));
		return a.cols = Array(o).fill({
			type: "align",
			align: n
		}), t ? {
			type: "leftright",
			mode: e.mode,
			body: [a],
			left: t[0],
			right: t[1],
			rightColor: void 0
		} : a;
	},
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: ["smallmatrix"],
	props: { numArgs: 0 },
	handler(e) {
		var t = Ms(e.parser, { arraystretch: .5 }, "script");
		return t.colSeparationType = "small", t;
	},
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: ["subarray"],
	props: { numArgs: 1 },
	handler(e, t) {
		var n = (bo(t[0]) ? [t[0]] : Z(t[0], "ordgroup").body).map(function(e) {
			var t = yo(e).text;
			if ("lc".includes(t)) return {
				type: "align",
				align: t
			};
			throw new N("Unknown column alignment: " + t, e);
		});
		if (n.length > 1) throw new N("{subarray} can contain only one column");
		var r = {
			cols: n,
			hskipBeforeAndAfter: !1,
			arraystretch: .5
		}, i = Ms(e.parser, r, "script");
		if (i.body.length > 0 && i.body[0].length > 1) throw new N("{subarray} can contain only one column");
		return i;
	},
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: [
		"cases",
		"dcases",
		"rcases",
		"drcases"
	],
	props: { numArgs: 0 },
	handler(e) {
		var t = Ms(e.parser, {
			arraystretch: 1.2,
			cols: [{
				type: "align",
				align: "l",
				pregap: 0,
				postgap: 1
			}, {
				type: "align",
				align: "l",
				pregap: 0,
				postgap: 0
			}]
		}, Ns(e.envName));
		return {
			type: "leftright",
			mode: e.mode,
			body: [t],
			left: e.envName.includes("r") ? "." : "\\{",
			right: e.envName.includes("r") ? "\\}" : ".",
			rightColor: void 0
		};
	},
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: [
		"align",
		"align*",
		"aligned",
		"split"
	],
	props: { numArgs: 0 },
	handler: Ls,
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: [
		"gathered",
		"gather",
		"gather*"
	],
	props: { numArgs: 0 },
	handler(e) {
		As.has(e.envName) && ks(e);
		var t = {
			cols: [{
				type: "align",
				align: "c"
			}],
			addJot: !0,
			colSeparationType: "gather",
			autoTag: js(e.envName),
			emptySingleRow: !0,
			leqno: e.parser.settings.leqno
		};
		return Ms(e.parser, t, "display");
	},
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: [
		"alignat",
		"alignat*",
		"alignedat"
	],
	props: { numArgs: 1 },
	handler: Ls,
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: ["equation", "equation*"],
	props: { numArgs: 0 },
	handler(e) {
		ks(e);
		var t = {
			autoTag: js(e.envName),
			emptySingleRow: !0,
			singleRow: !0,
			maxNumCols: 1,
			leqno: e.parser.settings.leqno
		};
		return Ms(e.parser, t, "display");
	},
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), ws({
	type: "array",
	names: ["CD"],
	props: { numArgs: 0 },
	handler(e) {
		return ks(e), Po(e.parser);
	},
	htmlBuilder: Ps,
	mathmlBuilder: Is
}), Q("\\nonumber", "\\gdef\\@eqnsw{0}"), Q("\\notag", "\\nonumber"), q({
	type: "text",
	names: ["\\hline", "\\hdashline"],
	props: {
		numArgs: 0,
		allowedInText: !0,
		allowedInMath: !0
	},
	handler(e, t) {
		throw new N(e.funcName + " valid only within array environment");
	}
});
var Rs = Cs;
q({
	type: "environment",
	names: ["\\begin", "\\end"],
	props: {
		numArgs: 1,
		argTypes: ["text"]
	},
	handler(e, t) {
		var { parser: n, funcName: r } = e, i = t[0];
		if (i.type !== "ordgroup") throw new N("Invalid environment name", i);
		for (var a = "", o = 0; o < i.body.length; ++o) a += Z(i.body[o], "textord").text;
		if (r === "\\begin") {
			if (!Rs.hasOwnProperty(a)) throw new N("No such environment: " + a, i);
			var s = Rs[a], { args: c, optArgs: l } = n.parseArguments("\\begin{" + a + "}", s), u = {
				mode: n.mode,
				envName: a,
				parser: n
			}, d = s.handler(u, c, l);
			n.expect("\\end", !1);
			var f = n.nextToken, p = Z(n.parseFunction(), "environment");
			if (p.name !== a) throw new N("Mismatch: \\begin{" + a + "} matched by \\end{" + p.name + "}", f);
			return d;
		}
		return {
			type: "environment",
			mode: n.mode,
			name: a,
			nameGroup: i
		};
	}
});
var zs = (e, t) => {
	var n = e.font, r = t.withFont(n);
	return Ba(e.body, r);
}, Bs = (e, t) => {
	var n = e.font, r = t.withFont(n);
	return Y(e.body, r);
}, Vs = {
	"\\Bbb": "\\mathbb",
	"\\bold": "\\mathbf",
	"\\frak": "\\mathfrak"
};
q({
	type: "font",
	names: [
		"\\mathrm",
		"\\mathit",
		"\\mathbf",
		"\\mathnormal",
		"\\mathsfit",
		"\\mathbb",
		"\\mathcal",
		"\\mathfrak",
		"\\mathscr",
		"\\mathsf",
		"\\mathtt",
		"\\Bbb",
		"\\bold",
		"\\frak"
	],
	props: {
		numArgs: 1,
		allowedInArgument: !0
	},
	handler: (e, t) => {
		var { parser: n, funcName: r } = e, i = Oa(t[0]), a = r;
		return a in Vs && (a = Vs[a]), {
			type: "font",
			mode: n.mode,
			font: a.slice(1),
			body: i
		};
	},
	htmlBuilder: zs,
	mathmlBuilder: Bs
}), q({
	type: "mclass",
	names: ["\\boldsymbol", "\\bm"],
	props: { numArgs: 1 },
	handler: (e, t) => {
		var { parser: n } = e, r = t[0];
		return {
			type: "mclass",
			mode: n.mode,
			mclass: Oo(r),
			body: [{
				type: "font",
				mode: n.mode,
				font: "boldsymbol",
				body: r
			}],
			isCharacterBox: Zn(r)
		};
	}
}), q({
	type: "font",
	names: [
		"\\rm",
		"\\sf",
		"\\tt",
		"\\bf",
		"\\it",
		"\\cal"
	],
	props: {
		numArgs: 0,
		allowedInText: !0
	},
	handler: (e, t) => {
		var { parser: n, funcName: r, breakOnTokenText: i } = e, { mode: a } = n, o = n.parseExpression(!0, i);
		return {
			type: "font",
			mode: a,
			font: "math" + r.slice(1),
			body: {
				type: "ordgroup",
				mode: n.mode,
				body: o
			}
		};
	},
	htmlBuilder: zs,
	mathmlBuilder: Bs
});
var Hs = (e, t) => {
	var n = t.style, r = n.fracNum(), i = n.fracDen(), a = t.havingStyle(r), o = Ba(e.numer, a, t);
	if (e.continued) {
		var s = 8.5 / t.fontMetrics().ptPerEm, c = 3.5 / t.fontMetrics().ptPerEm;
		o.height = o.height < s ? s : o.height, o.depth = o.depth < c ? c : o.depth;
	}
	a = t.havingStyle(i);
	var l = Ba(e.denom, a, t), u, d, f;
	e.hasBarLine ? (e.barSize ? (d = Hr(e.barSize, t), u = ca("frac-line", t, d)) : u = ca("frac-line", t), d = u.height, f = u.height) : (u = null, d = 0, f = t.fontMetrics().defaultRuleThickness);
	var p, m, h;
	n.size === P.DISPLAY.size ? (p = t.fontMetrics().num1, m = d > 0 ? 3 * f : 7 * f, h = t.fontMetrics().denom1) : (d > 0 ? (p = t.fontMetrics().num2, m = f) : (p = t.fontMetrics().num3, m = 3 * f), h = t.fontMetrics().denom2);
	var g;
	if (u) {
		var _ = t.fontMetrics().axisHeight;
		p - o.depth - (_ + .5 * d) < m && (p += m - (p - o.depth - (_ + .5 * d))), _ - .5 * d - (l.height - h) < m && (h += m - (_ - .5 * d - (l.height - h)));
		var v = -(_ - .5 * d);
		g = pa({
			positionType: "individualShift",
			children: [
				{
					type: "elem",
					elem: l,
					shift: h
				},
				{
					type: "elem",
					elem: u,
					shift: v
				},
				{
					type: "elem",
					elem: o,
					shift: -p
				}
			]
		});
	} else {
		var y = p - o.depth - (l.height - h);
		y < m && (p += .5 * (m - y), h += .5 * (m - y)), g = pa({
			positionType: "individualShift",
			children: [{
				type: "elem",
				elem: l,
				shift: h
			}, {
				type: "elem",
				elem: o,
				shift: -p
			}]
		});
	}
	a = t.havingStyle(n), g.height *= a.sizeMultiplier / t.sizeMultiplier, g.depth *= a.sizeMultiplier / t.sizeMultiplier;
	var b = n.size === P.DISPLAY.size ? t.fontMetrics().delim1 : n.size === P.SCRIPTSCRIPT.size ? t.havingStyle(P.SCRIPT).fontMetrics().delim2 : t.fontMetrics().delim2, x = e.leftDelim == null ? za(t, ["mopen"]) : ms(e.leftDelim, b, !0, t.havingStyle(n), e.mode, ["mopen"]), S = e.continued ? K([]) : e.rightDelim == null ? za(t, ["mclose"]) : ms(e.rightDelim, b, !0, t.havingStyle(n), e.mode, ["mclose"]);
	return K(["mord"].concat(a.sizingClasses(t)), [
		x,
		K(["mfrac"], [g]),
		S
	], t);
}, Us = (e, t) => {
	var n = new J("mfrac", [Y(e.numer, t), Y(e.denom, t)]);
	if (!e.hasBarLine) n.setAttribute("linethickness", "0px");
	else if (e.barSize) {
		var r = Hr(e.barSize, t);
		n.setAttribute("linethickness", F(r));
	}
	if (e.leftDelim != null || e.rightDelim != null) {
		var i = [];
		if (e.leftDelim != null) {
			var a = new J("mo", [new Wa(e.leftDelim.replace("\\", ""))]);
			a.setAttribute("fence", "true"), i.push(a);
		}
		if (i.push(n), e.rightDelim != null) {
			var o = new J("mo", [new Wa(e.rightDelim.replace("\\", ""))]);
			o.setAttribute("fence", "true"), i.push(o);
		}
		return Ya(i);
	}
	return n;
}, Ws = (e, t) => t ? {
	type: "styling",
	mode: e.mode,
	style: t,
	body: [e]
} : e;
q({
	type: "genfrac",
	names: [
		"\\cfrac",
		"\\dfrac",
		"\\frac",
		"\\tfrac",
		"\\dbinom",
		"\\binom",
		"\\tbinom",
		"\\\\atopfrac",
		"\\\\bracefrac",
		"\\\\brackfrac"
	],
	props: {
		numArgs: 2,
		allowedInArgument: !0
	},
	handler: (e, t) => {
		var { parser: n, funcName: r } = e, i = t[0], a = t[1], o, s = null, c = null;
		switch (r) {
			case "\\cfrac":
			case "\\dfrac":
			case "\\frac":
			case "\\tfrac":
				o = !0;
				break;
			case "\\\\atopfrac":
				o = !1;
				break;
			case "\\dbinom":
			case "\\binom":
			case "\\tbinom":
				o = !1, s = "(", c = ")";
				break;
			case "\\\\bracefrac":
				o = !1, s = "\\{", c = "\\}";
				break;
			case "\\\\brackfrac":
				o = !1, s = "[", c = "]";
				break;
			default: throw Error("Unrecognized genfrac command");
		}
		var l = r === "\\cfrac", u = null;
		return l || r.startsWith("\\d") ? u = "display" : r.startsWith("\\t") && (u = "text"), Ws({
			type: "genfrac",
			mode: n.mode,
			numer: i,
			denom: a,
			continued: l,
			hasBarLine: o,
			leftDelim: s,
			rightDelim: c,
			barSize: null
		}, u);
	},
	htmlBuilder: Hs,
	mathmlBuilder: Us
}), q({
	type: "infix",
	names: [
		"\\over",
		"\\choose",
		"\\atop",
		"\\brace",
		"\\brack"
	],
	props: {
		numArgs: 0,
		infix: !0
	},
	handler(e) {
		var { parser: t, funcName: n, token: r } = e, i;
		switch (n) {
			case "\\over":
				i = "\\frac";
				break;
			case "\\choose":
				i = "\\binom";
				break;
			case "\\atop":
				i = "\\\\atopfrac";
				break;
			case "\\brace":
				i = "\\\\bracefrac";
				break;
			case "\\brack":
				i = "\\\\brackfrac";
				break;
			default: throw Error("Unrecognized infix genfrac command");
		}
		return {
			type: "infix",
			mode: t.mode,
			replaceWith: i,
			token: r
		};
	}
});
var Gs = [
	"display",
	"text",
	"script",
	"scriptscript"
], Ks = function(e) {
	var t = null;
	return e.length > 0 && (t = e, t = t === "." ? null : t), t;
};
q({
	type: "genfrac",
	names: ["\\genfrac"],
	props: {
		numArgs: 6,
		allowedInArgument: !0,
		argTypes: [
			"math",
			"math",
			"size",
			"text",
			"math",
			"math"
		]
	},
	handler(e, t) {
		var { parser: n } = e, r = t[4], i = t[5], a = Oa(t[0]), o = a.type === "atom" && a.family === "open" ? Ks(a.text) : null, s = Oa(t[1]), c = s.type === "atom" && s.family === "close" ? Ks(s.text) : null, l = Z(t[2], "size"), u, d = null;
		l.isBlank ? u = !0 : (d = l.value, u = d.number > 0);
		var f = null, p = t[3];
		if (p.type === "ordgroup") {
			if (p.body.length > 0) {
				var m = Z(p.body[0], "textord");
				f = Gs[Number(m.text)];
			}
		} else p = Z(p, "textord"), f = Gs[Number(p.text)];
		return Ws({
			type: "genfrac",
			mode: n.mode,
			numer: r,
			denom: i,
			continued: !1,
			hasBarLine: u,
			barSize: d,
			leftDelim: o,
			rightDelim: c
		}, f);
	}
}), q({
	type: "infix",
	names: ["\\above"],
	props: {
		numArgs: 1,
		argTypes: ["size"],
		infix: !0
	},
	handler(e, t) {
		var { parser: n, funcName: r, token: i } = e;
		return {
			type: "infix",
			mode: n.mode,
			replaceWith: "\\\\abovefrac",
			size: Z(t[0], "size").value,
			token: i
		};
	}
}), q({
	type: "genfrac",
	names: ["\\\\abovefrac"],
	props: {
		numArgs: 3,
		argTypes: [
			"math",
			"size",
			"math"
		]
	},
	handler: (e, t) => {
		var { parser: n, funcName: r } = e, i = t[0], a = Z(t[1], "infix").size;
		if (!a) throw Error("\\\\abovefrac expected size, but got " + String(a));
		var o = t[2], s = a.number > 0;
		return {
			type: "genfrac",
			mode: n.mode,
			numer: i,
			denom: o,
			continued: !1,
			hasBarLine: s,
			barSize: a,
			leftDelim: null,
			rightDelim: null
		};
	}
});
var qs = (e, t) => {
	var n = t.style, r, i;
	e.type === "supsub" ? (r = e.sup ? Ba(e.sup, t.havingStyle(n.sup()), t) : Ba(e.sub, t.havingStyle(n.sub()), t), i = Z(e.base, "horizBrace")) : i = Z(e, "horizBrace");
	var a = Ba(i.base, t.havingBaseStyle(P.DISPLAY)), o = mo(i, t), s = i.isOver ? pa({
		positionType: "firstBaseline",
		children: [
			{
				type: "elem",
				elem: a
			},
			{
				type: "kern",
				size: .1
			},
			{
				type: "elem",
				elem: o,
				wrapperClasses: ["svg-align"]
			}
		]
	}) : pa({
		positionType: "bottom",
		positionData: a.depth + .1 + o.height,
		children: [
			{
				type: "elem",
				elem: o,
				wrapperClasses: ["svg-align"]
			},
			{
				type: "kern",
				size: .1
			},
			{
				type: "elem",
				elem: a
			}
		]
	});
	if (r) {
		var c = K(["minner", i.isOver ? "mover" : "munder"], [s], t);
		s = i.isOver ? pa({
			positionType: "firstBaseline",
			children: [
				{
					type: "elem",
					elem: c
				},
				{
					type: "kern",
					size: .2
				},
				{
					type: "elem",
					elem: r
				}
			]
		}) : pa({
			positionType: "bottom",
			positionData: c.depth + .2 + r.height + r.depth,
			children: [
				{
					type: "elem",
					elem: r
				},
				{
					type: "kern",
					size: .2
				},
				{
					type: "elem",
					elem: c
				}
			]
		});
	}
	return K(["minner", i.isOver ? "mover" : "munder"], [s], t);
};
q({
	type: "horizBrace",
	names: [
		"\\overbrace",
		"\\underbrace",
		"\\overbracket",
		"\\underbracket"
	],
	props: { numArgs: 1 },
	handler(e, t) {
		var { parser: n, funcName: r } = e;
		return {
			type: "horizBrace",
			mode: n.mode,
			label: r,
			isOver: r.includes("\\over"),
			base: t[0]
		};
	},
	htmlBuilder: qs,
	mathmlBuilder: (e, t) => {
		var n = uo(e.label);
		return new J(e.isOver ? "mover" : "munder", [Y(e.base, t), n]);
	}
}), q({
	type: "href",
	names: ["\\href"],
	props: {
		numArgs: 2,
		argTypes: ["url", "original"],
		allowedInText: !0
	},
	handler: (e, t) => {
		var { parser: n } = e, r = t[1], i = Z(t[0], "url").url;
		return n.settings.isTrusted({
			command: "\\href",
			url: i
		}) ? {
			type: "href",
			mode: n.mode,
			href: i,
			body: ka(r)
		} : n.formatUnsupportedCmd("\\href");
	},
	htmlBuilder: (e, t) => {
		var n = Pa(e.body, t, !1);
		return la(e.href, [], n, t);
	},
	mathmlBuilder: (e, t) => {
		var n = eo(e.body, t);
		return n instanceof J || (n = new J("mrow", [n])), n.setAttribute("href", e.href), n;
	}
}), q({
	type: "href",
	names: ["\\url"],
	props: {
		numArgs: 1,
		argTypes: ["url"],
		allowedInText: !0
	},
	handler: (e, t) => {
		var { parser: n } = e, r = Z(t[0], "url").url;
		if (!n.settings.isTrusted({
			command: "\\url",
			url: r
		})) return n.formatUnsupportedCmd("\\url");
		for (var i = [], a = 0; a < r.length; a++) {
			var o = r[a];
			o === "~" && (o = "\\textasciitilde"), i.push({
				type: "textord",
				mode: "text",
				text: o
			});
		}
		var s = {
			type: "text",
			mode: n.mode,
			font: "\\texttt",
			body: i
		};
		return {
			type: "href",
			mode: n.mode,
			href: r,
			body: ka(s)
		};
	}
}), q({
	type: "hbox",
	names: ["\\hbox"],
	props: {
		numArgs: 1,
		argTypes: ["text"],
		allowedInText: !0,
		primitive: !0
	},
	handler(e, t) {
		var { parser: n } = e;
		return {
			type: "hbox",
			mode: n.mode,
			body: ka(t[0])
		};
	},
	htmlBuilder(e, t) {
		return ua(Pa(e.body, t.withFont(""), !1));
	},
	mathmlBuilder(e, t) {
		return new J("mrow", $a(e.body, t.withFont("")));
	}
}), q({
	type: "html",
	names: [
		"\\htmlClass",
		"\\htmlId",
		"\\htmlStyle",
		"\\htmlData"
	],
	props: {
		numArgs: 2,
		argTypes: ["raw", "original"],
		allowedInText: !0
	},
	handler: (e, t) => {
		var { parser: n, funcName: r, token: i } = e, a = Z(t[0], "raw").string, o = t[1];
		n.settings.strict && n.settings.reportNonstrict("htmlExtension", "HTML extension is disabled on strict mode");
		var s, c = {};
		switch (r) {
			case "\\htmlClass":
				c.class = a, s = {
					command: "\\htmlClass",
					class: a
				};
				break;
			case "\\htmlId":
				c.id = a, s = {
					command: "\\htmlId",
					id: a
				};
				break;
			case "\\htmlStyle":
				c.style = a, s = {
					command: "\\htmlStyle",
					style: a
				};
				break;
			case "\\htmlData":
				for (var l = a.split(","), u = 0; u < l.length; u++) {
					var d = l[u], f = d.indexOf("=");
					if (f < 0) throw new N("\\htmlData key/value '" + d + "' missing equals sign");
					var p = d.slice(0, f), m = d.slice(f + 1);
					c["data-" + p.trim()] = m;
				}
				s = {
					command: "\\htmlData",
					attributes: c
				};
				break;
			default: throw Error("Unrecognized html command");
		}
		return n.settings.isTrusted(s) ? {
			type: "html",
			mode: n.mode,
			attributes: c,
			body: ka(o)
		} : n.formatUnsupportedCmd(r);
	},
	htmlBuilder: (e, t) => {
		var n = Pa(e.body, t, !1), r = ["enclosing"];
		e.attributes.class && r.push(...e.attributes.class.trim().split(/\s+/));
		var i = K(r, n, t);
		for (var a in e.attributes) a !== "class" && e.attributes.hasOwnProperty(a) && i.setAttribute(a, e.attributes[a]);
		return i;
	},
	mathmlBuilder: (e, t) => eo(e.body, t)
}), q({
	type: "htmlmathml",
	names: ["\\html@mathml"],
	props: {
		numArgs: 2,
		allowedInArgument: !0,
		allowedInText: !0
	},
	handler: (e, t) => {
		var { parser: n } = e;
		return {
			type: "htmlmathml",
			mode: n.mode,
			html: ka(t[0]),
			mathml: ka(t[1])
		};
	},
	htmlBuilder: (e, t) => ua(Pa(e.html, t, !1)),
	mathmlBuilder: (e, t) => eo(e.mathml, t)
});
var Js = function(e) {
	if (/^[-+]? *(\d+(\.\d*)?|\.\d+)$/.test(e)) return {
		number: +e,
		unit: "bp"
	};
	var t = /([-+]?) *(\d+(?:\.\d*)?|\.\d+) *([a-z]{2})/.exec(e);
	if (!t) throw new N("Invalid size: '" + e + "' in \\includegraphics");
	var n = {
		number: +(t[1] + t[2]),
		unit: t[3]
	};
	if (!Vr(n)) throw new N("Invalid unit: '" + n.unit + "' in \\includegraphics.");
	return n;
};
q({
	type: "includegraphics",
	names: ["\\includegraphics"],
	props: {
		numArgs: 1,
		numOptionalArgs: 1,
		argTypes: ["raw", "url"],
		allowedInText: !1
	},
	handler: (e, t, n) => {
		var { parser: r } = e, i = {
			number: 0,
			unit: "em"
		}, a = {
			number: .9,
			unit: "em"
		}, o = {
			number: 0,
			unit: "em"
		}, s = "";
		if (n[0]) for (var c = Z(n[0], "raw").string.split(","), l = 0; l < c.length; l++) {
			var u = c[l].split("=");
			if (u.length === 2) {
				var d = u[1].trim();
				switch (u[0].trim()) {
					case "alt":
						s = d;
						break;
					case "width":
						i = Js(d);
						break;
					case "height":
						a = Js(d);
						break;
					case "totalheight":
						o = Js(d);
						break;
					default: throw new N("Invalid key: '" + u[0] + "' in \\includegraphics.");
				}
			}
		}
		var f = Z(t[0], "url").url;
		return s === "" && (s = f, s = s.replace(/^.*[\\/]/, ""), s = s.substring(0, s.lastIndexOf("."))), r.settings.isTrusted({
			command: "\\includegraphics",
			url: f
		}) ? {
			type: "includegraphics",
			mode: r.mode,
			alt: s,
			width: i,
			height: a,
			totalheight: o,
			src: f
		} : r.formatUnsupportedCmd("\\includegraphics");
	},
	htmlBuilder: (e, t) => {
		var n = Hr(e.height, t), r = 0;
		e.totalheight.number > 0 && (r = Hr(e.totalheight, t) - n);
		var i = 0;
		e.width.number > 0 && (i = Hr(e.width, t));
		var a = { height: F(n + r) };
		i > 0 && (a.width = F(i)), r > 0 && (a.verticalAlign = F(-r));
		var o = new Zr(e.src, e.alt, a);
		return o.height = n, o.depth = r, o;
	},
	mathmlBuilder: (e, t) => {
		var n = new J("mglyph", []);
		n.setAttribute("alt", e.alt);
		var r = Hr(e.height, t), i = 0;
		if (e.totalheight.number > 0 && (i = Hr(e.totalheight, t) - r, n.setAttribute("valign", F(-i))), n.setAttribute("height", F(r + i)), e.width.number > 0) {
			var a = Hr(e.width, t);
			n.setAttribute("width", F(a));
		}
		return n.setAttribute("src", e.src), n;
	}
}), q({
	type: "kern",
	names: [
		"\\kern",
		"\\mkern",
		"\\hskip",
		"\\mskip"
	],
	props: {
		numArgs: 1,
		argTypes: ["size"],
		primitive: !0,
		allowedInText: !0
	},
	handler(e, t) {
		var { parser: n, funcName: r } = e, i = Z(t[0], "size");
		if (n.settings.strict) {
			var a = r[1] === "m", o = i.value.unit === "mu";
			a ? (o || n.settings.reportNonstrict("mathVsTextUnits", "LaTeX's " + r + " supports only mu units, " + ("not " + i.value.unit + " units")), n.mode !== "math" && n.settings.reportNonstrict("mathVsTextUnits", "LaTeX's " + r + " works only in math mode")) : o && n.settings.reportNonstrict("mathVsTextUnits", "LaTeX's " + r + " doesn't support mu units");
		}
		return {
			type: "kern",
			mode: n.mode,
			dimension: i.value
		};
	},
	htmlBuilder(e, t) {
		return ma(e.dimension, t);
	},
	mathmlBuilder(e, t) {
		return new Ga(Hr(e.dimension, t));
	}
}), q({
	type: "lap",
	names: [
		"\\mathllap",
		"\\mathrlap",
		"\\mathclap"
	],
	props: {
		numArgs: 1,
		allowedInText: !0
	},
	handler: (e, t) => {
		var { parser: n, funcName: r } = e, i = t[0];
		return {
			type: "lap",
			mode: n.mode,
			alignment: r.slice(5),
			body: i
		};
	},
	htmlBuilder: (e, t) => {
		var n;
		e.alignment === "clap" ? (n = K([], [Ba(e.body, t)]), n = K(["inner"], [n], t)) : n = K(["inner"], [Ba(e.body, t)]);
		var r = K(["fix"], []), i = K([e.alignment], [n, r], t), a = K(["strut"]);
		return a.style.height = F(i.height + i.depth), i.depth && (a.style.verticalAlign = F(-i.depth)), i.children.unshift(a), i = K(["thinbox"], [i], t), K(["mord", "vbox"], [i], t);
	},
	mathmlBuilder: (e, t) => {
		var n = new J("mpadded", [Y(e.body, t)]);
		if (e.alignment !== "rlap") {
			var r = e.alignment === "llap" ? "-1" : "-0.5";
			n.setAttribute("lspace", r + "width");
		}
		return n.setAttribute("width", "0px"), n;
	}
}), q({
	type: "styling",
	names: ["\\(", "$"],
	props: {
		numArgs: 0,
		allowedInText: !0,
		allowedInMath: !1
	},
	handler(e, t) {
		var { funcName: n, parser: r } = e, i = r.mode;
		r.switchMode("math");
		var a = n === "\\(" ? "\\)" : "$", o = r.parseExpression(!1, a);
		return r.expect(a), r.switchMode(i), {
			type: "styling",
			mode: r.mode,
			style: "text",
			resetFont: !0,
			body: o
		};
	}
}), q({
	type: "text",
	names: ["\\)", "\\]"],
	props: {
		numArgs: 0,
		allowedInText: !0,
		allowedInMath: !1
	},
	handler(e, t) {
		throw new N("Mismatched " + e.funcName);
	}
});
var Ys = (e, t) => {
	switch (t.style.size) {
		case P.DISPLAY.size: return e.display;
		case P.TEXT.size: return e.text;
		case P.SCRIPT.size: return e.script;
		case P.SCRIPTSCRIPT.size: return e.scriptscript;
		default: return e.text;
	}
};
q({
	type: "mathchoice",
	names: ["\\mathchoice"],
	props: {
		numArgs: 4,
		primitive: !0
	},
	handler: (e, t) => {
		var { parser: n } = e;
		return {
			type: "mathchoice",
			mode: n.mode,
			display: ka(t[0]),
			text: ka(t[1]),
			script: ka(t[2]),
			scriptscript: ka(t[3])
		};
	},
	htmlBuilder: (e, t) => ua(Pa(Ys(e, t), t, !1)),
	mathmlBuilder: (e, t) => eo(Ys(e, t), t)
});
var Xs = (e, t, n, r, i, a, o) => {
	e = K([], [e]);
	var s = n && Zn(n), c, l;
	if (t) {
		var u = Ba(t, r.havingStyle(i.sup()), r);
		l = {
			elem: u,
			kern: Math.max(r.fontMetrics().bigOpSpacing1, r.fontMetrics().bigOpSpacing3 - u.depth)
		};
	}
	if (n) {
		var d = Ba(n, r.havingStyle(i.sub()), r);
		c = {
			elem: d,
			kern: Math.max(r.fontMetrics().bigOpSpacing2, r.fontMetrics().bigOpSpacing4 - d.height)
		};
	}
	var f;
	if (l && c) f = pa({
		positionType: "bottom",
		positionData: r.fontMetrics().bigOpSpacing5 + c.elem.height + c.elem.depth + c.kern + e.depth + o,
		children: [
			{
				type: "kern",
				size: r.fontMetrics().bigOpSpacing5
			},
			{
				type: "elem",
				elem: c.elem,
				marginLeft: F(-a)
			},
			{
				type: "kern",
				size: c.kern
			},
			{
				type: "elem",
				elem: e
			},
			{
				type: "kern",
				size: l.kern
			},
			{
				type: "elem",
				elem: l.elem,
				marginLeft: F(a)
			},
			{
				type: "kern",
				size: r.fontMetrics().bigOpSpacing5
			}
		]
	});
	else if (c) f = pa({
		positionType: "top",
		positionData: e.height - o,
		children: [
			{
				type: "kern",
				size: r.fontMetrics().bigOpSpacing5
			},
			{
				type: "elem",
				elem: c.elem,
				marginLeft: F(-a)
			},
			{
				type: "kern",
				size: c.kern
			},
			{
				type: "elem",
				elem: e
			}
		]
	});
	else if (l) f = pa({
		positionType: "bottom",
		positionData: e.depth + o,
		children: [
			{
				type: "elem",
				elem: e
			},
			{
				type: "kern",
				size: l.kern
			},
			{
				type: "elem",
				elem: l.elem,
				marginLeft: F(a)
			},
			{
				type: "kern",
				size: r.fontMetrics().bigOpSpacing5
			}
		]
	});
	else return e;
	var p = [f];
	if (c && a !== 0 && !s) {
		var m = K(["mspace"], [], r);
		m.style.marginRight = F(a), p.unshift(m);
	}
	return K(["mop", "op-limits"], p, r);
}, Zs = new Set(["\\smallint"]), Qs = (e, t) => {
	var n, r, i = !1, a;
	e.type === "supsub" ? (n = e.sup, r = e.sub, a = Z(e.base, "op"), i = !0) : a = Z(e, "op");
	var o = t.style, s = !1;
	o.size === P.DISPLAY.size && a.symbol && !Zs.has(a.name) && (s = !0);
	var c, l;
	if (a.symbol) {
		var u = s ? "Size2-Regular" : "Size1-Regular", d = "";
		if ((a.name === "\\oiint" || a.name === "\\oiiint") && (d = a.name.slice(1), a.name = d === "oiint" ? "\\iint" : "\\iiint"), c = ea(a.name, u, "math", t, [
			"mop",
			"op-symbol",
			s ? "large-op" : "small-op"
		]), l = c.italic, d.length > 0) {
			var f = va(d + "Size" + (s ? "2" : "1"), t);
			c = pa({
				positionType: "individualShift",
				children: [{
					type: "elem",
					elem: c,
					shift: 0
				}, {
					type: "elem",
					elem: f,
					shift: s ? .08 : 0
				}]
			}), a.name = "\\" + d, c.classes.unshift("mop"), c.italic = l;
		}
	} else if (a.body) {
		var p = Pa(a.body, t, !0);
		p.length === 1 && p[0] instanceof $r ? (c = p[0], c.classes[0] = "mop") : c = K(["mop"], p, t);
	} else {
		for (var m = [], h = 1; h < a.name.length; h++) m.push(ta(a.name[h], a.mode, t));
		c = K(["mop"], m, t);
	}
	var g = 0, _ = 0;
	return (c instanceof $r || a.name === "\\oiint" || a.name === "\\oiiint") && !a.suppressBaseShift && (g = (c.height - c.depth) / 2 - t.fontMetrics().axisHeight, _ = c.italic ?? 0), i ? Xs(c, n, r, t, o, _, g) : (g && (c.style.position = "relative", c.style.top = F(g)), c);
}, $s = (e, t) => {
	var n;
	if (e.symbol) n = new J("mo", [Ja(e.name, e.mode)]), Zs.has(e.name) && n.setAttribute("largeop", "false");
	else if (e.body) n = new J("mo", $a(e.body, t));
	else {
		n = new J("mi", [new Wa(e.name.slice(1))]);
		var r = new J("mo", [Ja("⁡", "text")]);
		n = e.parentIsSupSub ? new J("mrow", [n, r]) : Ua([n, r]);
	}
	return n;
}, ec = {
	"∏": "\\prod",
	"∐": "\\coprod",
	"∑": "\\sum",
	"⋀": "\\bigwedge",
	"⋁": "\\bigvee",
	"⋂": "\\bigcap",
	"⋃": "\\bigcup",
	"⨀": "\\bigodot",
	"⨁": "\\bigoplus",
	"⨂": "\\bigotimes",
	"⨄": "\\biguplus",
	"⨆": "\\bigsqcup"
};
q({
	type: "op",
	names: /* @__PURE__ */ "\\coprod.\\bigvee.\\bigwedge.\\biguplus.\\bigcap.\\bigcup.\\intop.\\prod.\\sum.\\bigotimes.\\bigoplus.\\bigodot.\\bigsqcup.\\smallint.∏.∐.∑.⋀.⋁.⋂.⋃.⨀.⨁.⨂.⨄.⨆".split("."),
	props: { numArgs: 0 },
	handler: (e, t) => {
		var { parser: n, funcName: r } = e, i = r;
		return i.length === 1 && (i = ec[i]), {
			type: "op",
			mode: n.mode,
			limits: !0,
			parentIsSupSub: !1,
			symbol: !0,
			name: i
		};
	},
	htmlBuilder: Qs,
	mathmlBuilder: $s
}), q({
	type: "op",
	names: ["\\mathop"],
	props: {
		numArgs: 1,
		primitive: !0
	},
	handler: (e, t) => {
		var { parser: n } = e, r = t[0];
		return {
			type: "op",
			mode: n.mode,
			limits: !1,
			parentIsSupSub: !1,
			symbol: !1,
			body: ka(r)
		};
	},
	htmlBuilder: Qs,
	mathmlBuilder: $s
});
var tc = {
	"∫": "\\int",
	"∬": "\\iint",
	"∭": "\\iiint",
	"∮": "\\oint",
	"∯": "\\oiint",
	"∰": "\\oiiint"
};
q({
	type: "op",
	names: /* @__PURE__ */ "\\arcsin.\\arccos.\\arctan.\\arctg.\\arcctg.\\arg.\\ch.\\cos.\\cosec.\\cosh.\\cot.\\cotg.\\coth.\\csc.\\ctg.\\cth.\\deg.\\dim.\\exp.\\hom.\\ker.\\lg.\\ln.\\log.\\sec.\\sin.\\sinh.\\sh.\\tan.\\tanh.\\tg.\\th".split("."),
	props: { numArgs: 0 },
	handler(e) {
		var { parser: t, funcName: n } = e;
		return {
			type: "op",
			mode: t.mode,
			limits: !1,
			parentIsSupSub: !1,
			symbol: !1,
			name: n
		};
	},
	htmlBuilder: Qs,
	mathmlBuilder: $s
}), q({
	type: "op",
	names: [
		"\\det",
		"\\gcd",
		"\\inf",
		"\\lim",
		"\\max",
		"\\min",
		"\\Pr",
		"\\sup"
	],
	props: { numArgs: 0 },
	handler(e) {
		var { parser: t, funcName: n } = e;
		return {
			type: "op",
			mode: t.mode,
			limits: !0,
			parentIsSupSub: !1,
			symbol: !1,
			name: n
		};
	},
	htmlBuilder: Qs,
	mathmlBuilder: $s
}), q({
	type: "op",
	names: [
		"\\int",
		"\\iint",
		"\\iiint",
		"\\oint",
		"\\oiint",
		"\\oiiint",
		"∫",
		"∬",
		"∭",
		"∮",
		"∯",
		"∰"
	],
	props: {
		numArgs: 0,
		allowedInArgument: !0
	},
	handler(e) {
		var { parser: t, funcName: n } = e, r = n;
		return r.length === 1 && (r = tc[r]), {
			type: "op",
			mode: t.mode,
			limits: !1,
			parentIsSupSub: !1,
			symbol: !0,
			name: r
		};
	},
	htmlBuilder: Qs,
	mathmlBuilder: $s
});
var nc = (e, t) => {
	var n, r, i = !1, a;
	e.type === "supsub" ? (n = e.sup, r = e.sub, a = Z(e.base, "operatorname"), i = !0) : a = Z(e, "operatorname");
	var o;
	if (a.body.length > 0) {
		for (var s = Pa(a.body.map((e) => {
			var t = "text" in e ? e.text : void 0;
			return typeof t == "string" ? {
				type: "textord",
				mode: e.mode,
				text: t
			} : e;
		}), t.withFont("mathrm"), !0), c = 0; c < s.length; c++) {
			var l = s[c];
			l instanceof $r && (l.text = l.text.replace(/\u2212/, "-").replace(/\u2217/, "*"));
		}
		o = K(["mop"], s, t);
	} else o = K(["mop"], [], t);
	return i ? Xs(o, n, r, t, t.style, 0, 0) : o;
};
q({
	type: "operatorname",
	names: ["\\operatorname@", "\\operatornamewithlimits"],
	props: { numArgs: 1 },
	handler: (e, t) => {
		var { parser: n, funcName: r } = e, i = t[0];
		return {
			type: "operatorname",
			mode: n.mode,
			body: ka(i),
			alwaysHandleSupSub: r === "\\operatornamewithlimits",
			limits: !1,
			parentIsSupSub: !1
		};
	},
	htmlBuilder: nc,
	mathmlBuilder: (e, t) => {
		for (var n = $a(e.body, t.withFont("mathrm")), r = !0, i = 0; i < n.length; i++) {
			var a = n[i];
			if (!(a instanceof Ga)) if (a instanceof J) switch (a.type) {
				case "mi":
				case "mn":
				case "mspace":
				case "mtext": break;
				case "mo":
					var o = a.children[0];
					a.children.length === 1 && o instanceof Wa ? o.text = o.text.replace(/\u2212/, "-").replace(/\u2217/, "*") : r = !1;
					break;
				default: r = !1;
			}
			else r = !1;
		}
		r && (n = [new Wa(n.map((e) => e.toText()).join(""))]);
		var s = new J("mi", n);
		s.setAttribute("mathvariant", "normal");
		var c = new J("mo", [Ja("⁡", "text")]);
		return e.parentIsSupSub ? new J("mrow", [s, c]) : Ua([s, c]);
	}
}), Q("\\operatorname", "\\@ifstar\\operatornamewithlimits\\operatorname@"), Da({
	type: "ordgroup",
	htmlBuilder(e, t) {
		return e.semisimple ? ua(Pa(e.body, t, !1)) : K(["mord"], Pa(e.body, t, !0), t);
	},
	mathmlBuilder(e, t) {
		return eo(e.body, t, !0);
	}
}), q({
	type: "overline",
	names: ["\\overline"],
	props: { numArgs: 1 },
	handler(e, t) {
		var { parser: n } = e, r = t[0];
		return {
			type: "overline",
			mode: n.mode,
			body: r
		};
	},
	htmlBuilder(e, t) {
		var n = Ba(e.body, t.havingCrampedStyle()), r = ca("overline-line", t), i = t.fontMetrics().defaultRuleThickness;
		return K(["mord", "overline"], [pa({
			positionType: "firstBaseline",
			children: [
				{
					type: "elem",
					elem: n
				},
				{
					type: "kern",
					size: 3 * i
				},
				{
					type: "elem",
					elem: r
				},
				{
					type: "kern",
					size: i
				}
			]
		})], t);
	},
	mathmlBuilder(e, t) {
		var n = new J("mo", [new Wa("‾")]);
		n.setAttribute("stretchy", "true");
		var r = new J("mover", [Y(e.body, t), n]);
		return r.setAttribute("accent", "true"), r;
	}
}), q({
	type: "phantom",
	names: ["\\phantom"],
	props: {
		numArgs: 1,
		allowedInText: !0
	},
	handler: (e, t) => {
		var { parser: n } = e, r = t[0];
		return {
			type: "phantom",
			mode: n.mode,
			body: ka(r)
		};
	},
	htmlBuilder: (e, t) => ua(Pa(e.body, t.withPhantom(), !1)),
	mathmlBuilder: (e, t) => new J("mphantom", $a(e.body, t))
}), Q("\\hphantom", "\\smash{\\phantom{#1}}"), q({
	type: "vphantom",
	names: ["\\vphantom"],
	props: {
		numArgs: 1,
		allowedInText: !0
	},
	handler: (e, t) => {
		var { parser: n } = e, r = t[0];
		return {
			type: "vphantom",
			mode: n.mode,
			body: r
		};
	},
	htmlBuilder: (e, t) => K(["mord", "rlap"], [K(["inner"], [Ba(e.body, t.withPhantom())]), K(["fix"], [])], t),
	mathmlBuilder: (e, t) => {
		var n = new J("mpadded", [new J("mphantom", $a(ka(e.body), t))]);
		return n.setAttribute("width", "0px"), n;
	}
}), q({
	type: "raisebox",
	names: ["\\raisebox"],
	props: {
		numArgs: 2,
		argTypes: ["size", "hbox"],
		allowedInText: !0
	},
	handler(e, t) {
		var { parser: n } = e, r = Z(t[0], "size").value, i = t[1];
		return {
			type: "raisebox",
			mode: n.mode,
			dy: r,
			body: i
		};
	},
	htmlBuilder(e, t) {
		var n = Ba(e.body, t);
		return pa({
			positionType: "shift",
			positionData: -Hr(e.dy, t),
			children: [{
				type: "elem",
				elem: n
			}]
		});
	},
	mathmlBuilder(e, t) {
		var n = new J("mpadded", [Y(e.body, t)]), r = e.dy.number + e.dy.unit;
		return n.setAttribute("voffset", r), n;
	}
}), q({
	type: "internal",
	names: ["\\relax"],
	props: {
		numArgs: 0,
		allowedInText: !0,
		allowedInArgument: !0
	},
	handler(e) {
		var { parser: t } = e;
		return {
			type: "internal",
			mode: t.mode
		};
	}
}), q({
	type: "rule",
	names: ["\\rule"],
	props: {
		numArgs: 2,
		numOptionalArgs: 1,
		allowedInText: !0,
		allowedInMath: !0,
		argTypes: [
			"size",
			"size",
			"size"
		]
	},
	handler(e, t, n) {
		var { parser: r } = e, i = n[0], a = Z(t[0], "size"), o = Z(t[1], "size");
		return {
			type: "rule",
			mode: r.mode,
			shift: i && Z(i, "size").value,
			width: a.value,
			height: o.value
		};
	},
	htmlBuilder(e, t) {
		var n = K(["mord", "rule"], [], t), r = Hr(e.width, t), i = Hr(e.height, t), a = e.shift ? Hr(e.shift, t) : 0;
		return n.style.borderRightWidth = F(r), n.style.borderTopWidth = F(i), n.style.bottom = F(a), n.width = r, n.height = i + a, n.depth = -a, n.maxFontSize = i * 1.125 * t.sizeMultiplier, n;
	},
	mathmlBuilder(e, t) {
		var n = Hr(e.width, t), r = Hr(e.height, t), i = e.shift ? Hr(e.shift, t) : 0, a = t.color && t.getColor() || "black", o = new J("mspace");
		o.setAttribute("mathbackground", a), o.setAttribute("width", F(n)), o.setAttribute("height", F(r));
		var s = new J("mpadded", [o]);
		return i >= 0 ? s.setAttribute("height", F(i)) : (s.setAttribute("height", F(i)), s.setAttribute("depth", F(-i))), s.setAttribute("voffset", F(i)), s;
	}
});
function rc(e, t, n) {
	for (var r = Pa(e, t, !1), i = t.sizeMultiplier / n.sizeMultiplier, a = 0; a < r.length; a++) {
		var o = r[a].classes.indexOf("sizing");
		o < 0 ? Array.prototype.push.apply(r[a].classes, t.sizingClasses(n)) : r[a].classes[o + 1] === "reset-size" + t.size && (r[a].classes[o + 1] = "reset-size" + n.size), r[a].height *= i, r[a].depth *= i;
	}
	return ua(r);
}
var ic = [
	"\\tiny",
	"\\sixptsize",
	"\\scriptsize",
	"\\footnotesize",
	"\\small",
	"\\normalsize",
	"\\large",
	"\\Large",
	"\\LARGE",
	"\\huge",
	"\\Huge"
];
q({
	type: "sizing",
	names: ic,
	props: {
		numArgs: 0,
		allowedInText: !0
	},
	handler: (e, t) => {
		var { breakOnTokenText: n, funcName: r, parser: i } = e, a = i.parseExpression(!1, n);
		return {
			type: "sizing",
			mode: i.mode,
			size: ic.indexOf(r) + 1,
			body: a
		};
	},
	htmlBuilder: (e, t) => {
		var n = t.havingSize(e.size);
		return rc(e.body, n, t);
	},
	mathmlBuilder: (e, t) => {
		var n = t.havingSize(e.size), r = new J("mstyle", $a(e.body, n));
		return r.setAttribute("mathsize", F(n.sizeMultiplier)), r;
	}
}), q({
	type: "smash",
	names: ["\\smash"],
	props: {
		numArgs: 1,
		numOptionalArgs: 1,
		allowedInText: !0
	},
	handler: (e, t, n) => {
		var { parser: r } = e, i = !1, a = !1, o = n[0] && Z(n[0], "ordgroup");
		if (o) for (var s, c = 0; c < o.body.length; ++c) {
			var l = o.body[c];
			if (s = yo(l).text, s === "t") i = !0;
			else if (s === "b") a = !0;
			else {
				i = !1, a = !1;
				break;
			}
		}
		else i = !0, a = !0;
		var u = t[0];
		return {
			type: "smash",
			mode: r.mode,
			body: u,
			smashHeight: i,
			smashDepth: a
		};
	},
	htmlBuilder: (e, t) => {
		var n = K([], [Ba(e.body, t)]);
		if (!e.smashHeight && !e.smashDepth) return n;
		if (e.smashHeight && (n.height = 0), e.smashDepth && (n.depth = 0), e.smashHeight && e.smashDepth) return K(["mord", "smash"], [n], t);
		if (n.children) for (var r = 0; r < n.children.length; r++) e.smashHeight && (n.children[r].height = 0), e.smashDepth && (n.children[r].depth = 0);
		return K(["mord"], [pa({
			positionType: "firstBaseline",
			children: [{
				type: "elem",
				elem: n
			}]
		})], t);
	},
	mathmlBuilder: (e, t) => {
		var n = new J("mpadded", [Y(e.body, t)]);
		return e.smashHeight && n.setAttribute("height", "0px"), e.smashDepth && n.setAttribute("depth", "0px"), n;
	}
}), q({
	type: "sqrt",
	names: ["\\sqrt"],
	props: {
		numArgs: 1,
		numOptionalArgs: 1
	},
	handler(e, t, n) {
		var { parser: r } = e, i = n[0], a = t[0];
		return {
			type: "sqrt",
			mode: r.mode,
			body: a,
			index: i
		};
	},
	htmlBuilder(e, t) {
		var n = Ba(e.body, t.havingCrampedStyle());
		n.height === 0 && (n.height = t.fontMetrics().xHeight), n = da(n, t);
		var r = t.fontMetrics().defaultRuleThickness, i = r;
		t.style.id < P.TEXT.id && (i = t.fontMetrics().xHeight);
		var a = r + i / 4, { span: o, ruleWidth: s, advanceWidth: c } = rs(n.height + n.depth + a + r, t), l = o.height - s;
		l > n.height + n.depth + a && (a = (a + l - n.height - n.depth) / 2);
		var u = o.height - n.height - a - s;
		n.style.paddingLeft = F(c);
		var d = pa({
			positionType: "firstBaseline",
			children: [
				{
					type: "elem",
					elem: n,
					wrapperClasses: ["svg-align"]
				},
				{
					type: "kern",
					size: -(n.height + u)
				},
				{
					type: "elem",
					elem: o
				},
				{
					type: "kern",
					size: s
				}
			]
		});
		if (e.index) {
			var f = t.havingStyle(P.SCRIPTSCRIPT), p = Ba(e.index, f, t);
			return K(["mord", "sqrt"], [K(["root"], [pa({
				positionType: "shift",
				positionData: -(.6 * (d.height - d.depth)),
				children: [{
					type: "elem",
					elem: p
				}]
			})]), d], t);
		} else return K(["mord", "sqrt"], [d], t);
	},
	mathmlBuilder(e, t) {
		var { body: n, index: r } = e;
		return r ? new J("mroot", [Y(n, t), Y(r, t)]) : new J("msqrt", [Y(n, t)]);
	}
});
var ac = {
	display: P.DISPLAY,
	text: P.TEXT,
	script: P.SCRIPT,
	scriptscript: P.SCRIPTSCRIPT
};
function oc(e) {
	return e in ac;
}
q({
	type: "styling",
	names: [
		"\\displaystyle",
		"\\textstyle",
		"\\scriptstyle",
		"\\scriptscriptstyle"
	],
	props: {
		numArgs: 0,
		allowedInText: !0,
		primitive: !0
	},
	handler(e, t) {
		var { breakOnTokenText: n, funcName: r, parser: i } = e, a = i.parseExpression(!0, n), o = r.slice(1, r.length - 5);
		if (!oc(o)) throw Error("Unknown style: " + o);
		return {
			type: "styling",
			mode: i.mode,
			style: o,
			body: a
		};
	},
	htmlBuilder(e, t) {
		var n = ac[e.style], r = t.havingStyle(n);
		return e.resetFont && (r = r.withFont("")), rc(e.body, r, t);
	},
	mathmlBuilder(e, t) {
		var n = ac[e.style], r = t.havingStyle(n);
		e.resetFont && (r = r.withFont(""));
		var i = new J("mstyle", $a(e.body, r)), a = {
			display: ["0", "true"],
			text: ["0", "false"],
			script: ["1", "false"],
			scriptscript: ["2", "false"]
		}[e.style];
		return i.setAttribute("scriptlevel", a[0]), i.setAttribute("displaystyle", a[1]), i;
	}
});
var sc = function(e, t) {
	var n = e.base;
	return n ? n.type === "op" ? n.limits && (t.style.size === P.DISPLAY.size || n.alwaysHandleSupSub) ? Qs : null : n.type === "operatorname" ? n.alwaysHandleSupSub && (t.style.size === P.DISPLAY.size || n.limits) ? nc : null : n.type === "accent" ? Zn(n.base) ? So : null : n.type === "horizBrace" && !e.sub === n.isOver ? qs : null : null;
};
Da({
	type: "supsub",
	htmlBuilder(e, t) {
		var n = sc(e, t);
		if (n) return n(e, t);
		var { base: r, sup: i, sub: a } = e, o = Ba(r, t), s, c, l = t.fontMetrics(), u = 0, d = 0, f = r && Zn(r);
		if (i) {
			var p = t.havingStyle(t.style.sup());
			s = Ba(i, p, t), f || (u = o.height - p.fontMetrics().supDrop * p.sizeMultiplier / t.sizeMultiplier);
		}
		if (a) {
			var m = t.havingStyle(t.style.sub());
			c = Ba(a, m, t), f || (d = o.depth + m.fontMetrics().subDrop * m.sizeMultiplier / t.sizeMultiplier);
		}
		var h = t.style === P.DISPLAY ? l.sup1 : t.style.cramped ? l.sup3 : l.sup2, g = t.sizeMultiplier, _ = F(.5 / l.ptPerEm / g), v = null;
		if (c) {
			var y = e.base && e.base.type === "op" && e.base.name && (e.base.name === "\\oiint" || e.base.name === "\\oiiint");
			(o instanceof $r || y) && (v = F(-(o.italic ?? 0)));
		}
		var b;
		if (s && c) {
			u = Math.max(u, h, s.depth + .25 * l.xHeight), d = Math.max(d, l.sub2);
			var x = 4 * l.defaultRuleThickness;
			if (u - s.depth - (c.height - d) < x) {
				d = x - (u - s.depth) + c.height;
				var S = .8 * l.xHeight - (u - s.depth);
				S > 0 && (u += S, d -= S);
			}
			b = pa({
				positionType: "individualShift",
				children: [{
					type: "elem",
					elem: c,
					shift: d,
					marginRight: _,
					marginLeft: v
				}, {
					type: "elem",
					elem: s,
					shift: -u,
					marginRight: _
				}]
			});
		} else if (c) d = Math.max(d, l.sub1, c.height - .8 * l.xHeight), b = pa({
			positionType: "shift",
			positionData: d,
			children: [{
				type: "elem",
				elem: c,
				marginLeft: v,
				marginRight: _
			}]
		});
		else if (s) u = Math.max(u, h, s.depth + .25 * l.xHeight), b = pa({
			positionType: "shift",
			positionData: -u,
			children: [{
				type: "elem",
				elem: s,
				marginRight: _
			}]
		});
		else throw Error("supsub must have either sup or sub.");
		return K([Ra(o, "right") || "mord"], [o, K(["msupsub"], [b])], t);
	},
	mathmlBuilder(e, t) {
		var n = !1, r, i;
		e.base && e.base.type === "horizBrace" && (i = !!e.sup, i === e.base.isOver && (n = !0, r = e.base.isOver)), e.base && (e.base.type === "op" || e.base.type === "operatorname") && (e.base.parentIsSupSub = !0);
		var a = [Y(e.base, t)];
		e.sub && a.push(Y(e.sub, t)), e.sup && a.push(Y(e.sup, t));
		var o;
		if (n) o = r ? "mover" : "munder";
		else if (!e.sub) {
			var s = e.base;
			o = s && s.type === "op" && s.limits && (t.style === P.DISPLAY || s.alwaysHandleSupSub) || s && s.type === "operatorname" && s.alwaysHandleSupSub && (s.limits || t.style === P.DISPLAY) ? "mover" : "msup";
		} else if (e.sup) {
			var c = e.base;
			o = c && c.type === "op" && c.limits && t.style === P.DISPLAY || c && c.type === "operatorname" && c.alwaysHandleSupSub && (t.style === P.DISPLAY || c.limits) ? "munderover" : "msubsup";
		} else {
			var l = e.base;
			o = l && l.type === "op" && l.limits && (t.style === P.DISPLAY || l.alwaysHandleSupSub) || l && l.type === "operatorname" && l.alwaysHandleSupSub && (l.limits || t.style === P.DISPLAY) ? "munder" : "msub";
		}
		return new J(o, a);
	}
}), Da({
	type: "atom",
	htmlBuilder(e, t) {
		return ta(e.text, e.mode, t, ["m" + e.family]);
	},
	mathmlBuilder(e, t) {
		var n = new J("mo", [Ja(e.text, e.mode)]);
		if (e.family === "bin") {
			var r = Za(e, t);
			r === "bold-italic" && n.setAttribute("mathvariant", r);
		} else e.family === "punct" ? n.setAttribute("separator", "true") : (e.family === "open" || e.family === "close") && n.setAttribute("stretchy", "false");
		return n;
	}
});
var cc = {
	mi: "italic",
	mn: "normal",
	mtext: "normal"
};
Da({
	type: "mathord",
	htmlBuilder(e, t) {
		return ra(e, t, "mathord");
	},
	mathmlBuilder(e, t) {
		var n = new J("mi", [Ja(e.text, e.mode, t)]), r = Za(e, t) || "italic";
		return r !== cc[n.type] && n.setAttribute("mathvariant", r), n;
	}
}), Da({
	type: "textord",
	htmlBuilder(e, t) {
		return ra(e, t, "textord");
	},
	mathmlBuilder(e, t) {
		var n = Ja(e.text, e.mode, t), r = Za(e, t) || "normal", i = e.mode === "text" ? new J("mtext", [n]) : /[0-9]/.test(e.text) ? new J("mn", [n]) : e.text === "\\prime" ? new J("mo", [n]) : new J("mi", [n]);
		return r !== cc[i.type] && i.setAttribute("mathvariant", r), i;
	}
});
var lc = {
	"\\nobreak": "nobreak",
	"\\allowbreak": "allowbreak"
}, uc = {
	" ": {},
	"\\ ": {},
	"~": { className: "nobreak" },
	"\\space": {},
	"\\nobreakspace": { className: "nobreak" }
};
Da({
	type: "spacing",
	htmlBuilder(e, t) {
		if (uc.hasOwnProperty(e.text)) {
			var n = uc[e.text].className || "";
			if (e.mode === "text") {
				var r = ra(e, t, "textord");
				return r.classes.push(n), r;
			} else return K(["mspace", n], [ta(e.text, e.mode, t)], t);
		} else if (lc.hasOwnProperty(e.text)) return K(["mspace", lc[e.text]], [], t);
		else throw new N("Unknown type of space \"" + e.text + "\"");
	},
	mathmlBuilder(e, t) {
		var n;
		if (uc.hasOwnProperty(e.text)) n = new J("mtext", [new Wa("\xA0")]);
		else if (lc.hasOwnProperty(e.text)) return new J("mspace");
		else throw new N("Unknown type of space \"" + e.text + "\"");
		return n;
	}
});
var dc = () => {
	var e = new J("mtd", []);
	return e.setAttribute("width", "50%"), e;
};
Da({
	type: "tag",
	mathmlBuilder(e, t) {
		var n = new J("mtable", [new J("mtr", [
			dc(),
			new J("mtd", [eo(e.body, t)]),
			dc(),
			new J("mtd", [eo(e.tag, t)])
		])]);
		return n.setAttribute("width", "100%"), n;
	}
});
var fc = {
	"\\text": void 0,
	"\\textrm": "textrm",
	"\\textsf": "textsf",
	"\\texttt": "texttt",
	"\\textnormal": "textrm"
}, pc = {
	"\\textbf": "textbf",
	"\\textmd": "textmd"
}, mc = {
	"\\textit": "textit",
	"\\textup": "textup"
}, hc = (e, t) => {
	var n = e.font;
	return n ? fc[n] ? t.withTextFontFamily(fc[n]) : pc[n] ? t.withTextFontWeight(pc[n]) : n === "\\emph" ? t.fontShape === "textit" ? t.withTextFontShape("textup") : t.withTextFontShape("textit") : t.withTextFontShape(mc[n]) : t;
};
q({
	type: "text",
	names: [
		"\\text",
		"\\textrm",
		"\\textsf",
		"\\texttt",
		"\\textnormal",
		"\\textbf",
		"\\textmd",
		"\\textit",
		"\\textup",
		"\\emph"
	],
	props: {
		numArgs: 1,
		argTypes: ["text"],
		allowedInArgument: !0,
		allowedInText: !0
	},
	handler(e, t) {
		var { parser: n, funcName: r } = e, i = t[0];
		return {
			type: "text",
			mode: n.mode,
			body: ka(i),
			font: r
		};
	},
	htmlBuilder(e, t) {
		var n = hc(e, t);
		return K(["mord", "text"], Pa(e.body, n, !0), n);
	},
	mathmlBuilder(e, t) {
		var n = hc(e, t);
		return eo(e.body, n);
	}
}), q({
	type: "underline",
	names: ["\\underline"],
	props: {
		numArgs: 1,
		allowedInText: !0
	},
	handler(e, t) {
		var { parser: n } = e;
		return {
			type: "underline",
			mode: n.mode,
			body: t[0]
		};
	},
	htmlBuilder(e, t) {
		var n = Ba(e.body, t), r = ca("underline-line", t), i = t.fontMetrics().defaultRuleThickness;
		return K(["mord", "underline"], [pa({
			positionType: "top",
			positionData: n.height,
			children: [
				{
					type: "kern",
					size: i
				},
				{
					type: "elem",
					elem: r
				},
				{
					type: "kern",
					size: 3 * i
				},
				{
					type: "elem",
					elem: n
				}
			]
		})], t);
	},
	mathmlBuilder(e, t) {
		var n = new J("mo", [new Wa("‾")]);
		n.setAttribute("stretchy", "true");
		var r = new J("munder", [Y(e.body, t), n]);
		return r.setAttribute("accentunder", "true"), r;
	}
}), q({
	type: "vcenter",
	names: ["\\vcenter"],
	props: {
		numArgs: 1,
		argTypes: ["original"],
		allowedInText: !1
	},
	handler(e, t) {
		var { parser: n } = e;
		return {
			type: "vcenter",
			mode: n.mode,
			body: t[0]
		};
	},
	htmlBuilder(e, t) {
		var n = Ba(e.body, t), r = t.fontMetrics().axisHeight;
		return pa({
			positionType: "shift",
			positionData: .5 * (n.height - r - (n.depth + r)),
			children: [{
				type: "elem",
				elem: n
			}]
		});
	},
	mathmlBuilder(e, t) {
		return new J("mrow", [new J("mpadded", [Y(e.body, t)], ["vcenter"])]);
	}
}), q({
	type: "verb",
	names: ["\\verb"],
	props: {
		numArgs: 0,
		allowedInText: !0
	},
	handler(e, t, n) {
		throw new N("\\verb ended by end of line instead of matching delimiter");
	},
	htmlBuilder(e, t) {
		for (var n = gc(e), r = [], i = t.havingStyle(t.style.text()), a = 0; a < n.length; a++) {
			var o = n[a];
			o === "~" && (o = "\\textasciitilde"), r.push(ea(o, "Typewriter-Regular", e.mode, i, ["mord", "texttt"]));
		}
		return K(["mord", "text"].concat(i.sizingClasses(t)), aa(r), i);
	},
	mathmlBuilder(e, t) {
		var n = new J("mtext", [new Wa(gc(e))]);
		return n.setAttribute("mathvariant", "monospace"), n;
	}
});
var gc = (e) => e.body.replace(/ /g, e.star ? "␣" : "\xA0"), _c = wa, vc = "[ \r\n	]", yc = "\\\\[a-zA-Z@]+", bc = "\\\\[^\ud800-\udfff]", xc = "(" + yc + ")" + vc + "*", Sc = "\\\\(\n|[ \r	]+\n?)[ \r	]*", Cc = "[̀-ͯ]", wc = RegExp(Cc + "+$"), Tc = "(" + vc + "+)|" + (Sc + "|") + "([!-\\[\\]-‧‪-퟿豈-￿]" + (Cc + "*") + "|[\ud800-\udbff][\udc00-\udfff]" + (Cc + "*") + "|\\\\verb\\*([^]).*?\\4|\\\\verb([^*a-zA-Z]).*?\\5" + ("|" + xc) + ("|" + bc + ")"), Ec = class {
	constructor(e, t) {
		this.input = void 0, this.settings = void 0, this.tokenRegex = void 0, this.catcodes = void 0, this.input = e, this.settings = t, this.tokenRegex = new RegExp(Tc, "g"), this.catcodes = {
			"%": 14,
			"~": 13
		};
	}
	setCatcode(e, t) {
		this.catcodes[e] = t;
	}
	lex() {
		var e = this.input, t = this.tokenRegex.lastIndex;
		if (t === e.length) return new Ds("EOF", new Es(this, t, t));
		var n = this.tokenRegex.exec(e);
		if (n === null || n.index !== t) throw new N("Unexpected character: '" + e[t] + "'", new Ds(e[t], new Es(this, t, t + 1)));
		var r = n[6] || n[3] || (n[2] ? "\\ " : " ");
		if (this.catcodes[r] === 14) {
			var i = e.indexOf("\n", this.tokenRegex.lastIndex);
			return i === -1 ? (this.tokenRegex.lastIndex = e.length, this.settings.reportNonstrict("commentAtEnd", "% comment has no terminating newline; LaTeX would fail because of commenting the end of math mode (e.g. $)")) : this.tokenRegex.lastIndex = i + 1, this.lex();
		}
		return new Ds(r, new Es(this, t, this.tokenRegex.lastIndex));
	}
}, Dc = class {
	constructor(e, t) {
		e === void 0 && (e = {}), t === void 0 && (t = {}), this.current = void 0, this.builtins = void 0, this.undefStack = void 0, this.current = t, this.builtins = e, this.undefStack = [];
	}
	beginGroup() {
		this.undefStack.push({});
	}
	endGroup() {
		if (this.undefStack.length === 0) throw new N("Unbalanced namespace destruction: attempt to pop global namespace; please report this as a bug");
		var e = this.undefStack.pop();
		for (var t in e) e.hasOwnProperty(t) && (e[t] == null ? delete this.current[t] : this.current[t] = e[t]);
	}
	endGroups() {
		for (; this.undefStack.length > 0;) this.endGroup();
	}
	has(e) {
		return this.current.hasOwnProperty(e) || this.builtins.hasOwnProperty(e);
	}
	get(e) {
		return this.current.hasOwnProperty(e) ? this.current[e] : this.builtins[e];
	}
	set(e, t, n) {
		if (n === void 0 && (n = !1), n) {
			for (var r = 0; r < this.undefStack.length; r++) delete this.undefStack[r][e];
			this.undefStack.length > 0 && (this.undefStack[this.undefStack.length - 1][e] = t);
		} else {
			var i = this.undefStack[this.undefStack.length - 1];
			i && !i.hasOwnProperty(e) && (i[e] = this.current[e]);
		}
		t == null ? delete this.current[e] : this.current[e] = t;
	}
}, Oc = Ts;
Q("\\noexpand", function(e) {
	var t = e.popToken();
	return e.isExpandable(t.text) && (t.noexpand = !0, t.treatAsRelax = !0), {
		tokens: [t],
		numArgs: 0
	};
}), Q("\\expandafter", function(e) {
	var t = e.popToken();
	return e.expandOnce(!0), {
		tokens: [t],
		numArgs: 0
	};
}), Q("\\@firstoftwo", function(e) {
	return {
		tokens: e.consumeArgs(2)[0],
		numArgs: 0
	};
}), Q("\\@secondoftwo", function(e) {
	return {
		tokens: e.consumeArgs(2)[1],
		numArgs: 0
	};
}), Q("\\@ifnextchar", function(e) {
	var t = e.consumeArgs(3);
	e.consumeSpaces();
	var n = e.future();
	return t[0].length === 1 && t[0][0].text === n.text ? {
		tokens: t[1],
		numArgs: 0
	} : {
		tokens: t[2],
		numArgs: 0
	};
}), Q("\\@ifstar", "\\@ifnextchar *{\\@firstoftwo{#1}}"), Q("\\TextOrMath", function(e) {
	var t = e.consumeArgs(2);
	return e.mode === "text" ? {
		tokens: t[0],
		numArgs: 0
	} : {
		tokens: t[1],
		numArgs: 0
	};
});
var kc = {
	0: 0,
	1: 1,
	2: 2,
	3: 3,
	4: 4,
	5: 5,
	6: 6,
	7: 7,
	8: 8,
	9: 9,
	a: 10,
	A: 10,
	b: 11,
	B: 11,
	c: 12,
	C: 12,
	d: 13,
	D: 13,
	e: 14,
	E: 14,
	f: 15,
	F: 15
};
Q("\\char", function(e) {
	var t = e.popToken(), n, r = 0;
	if (t.text === "'") n = 8, t = e.popToken();
	else if (t.text === "\"") n = 16, t = e.popToken();
	else if (t.text === "`") if (t = e.popToken(), t.text[0] === "\\") r = t.text.charCodeAt(1);
	else if (t.text === "EOF") throw new N("\\char` missing argument");
	else r = t.text.charCodeAt(0);
	else n = 10;
	if (n) {
		if (r = kc[t.text], r == null || r >= n) throw new N("Invalid base-" + n + " digit " + t.text);
		for (var i; (i = kc[e.future().text]) != null && i < n;) r *= n, r += i, e.popToken();
	}
	return "\\@char{" + r + "}";
});
var Ac = (e, t, n, r) => {
	var i = e.consumeArg().tokens;
	if (i.length !== 1) throw new N("\\newcommand's first argument must be a macro name");
	var a = i[0].text, o = e.isDefined(a);
	if (o && !t) throw new N("\\newcommand{" + a + "} attempting to redefine " + (a + "; use \\renewcommand"));
	if (!o && !n) throw new N("\\renewcommand{" + a + "} when command " + a + " does not yet exist; use \\newcommand");
	var s = 0;
	if (i = e.consumeArg().tokens, i.length === 1 && i[0].text === "[") {
		for (var c = "", l = e.expandNextToken(); l.text !== "]" && l.text !== "EOF";) c += l.text, l = e.expandNextToken();
		if (!c.match(/^\s*[0-9]+\s*$/)) throw new N("Invalid number of arguments: " + c);
		s = parseInt(c), i = e.consumeArg().tokens;
	}
	return o && r || e.macros.set(a, {
		tokens: i,
		numArgs: s
	}), "";
};
Q("\\newcommand", (e) => Ac(e, !1, !0, !1)), Q("\\renewcommand", (e) => Ac(e, !0, !1, !1)), Q("\\providecommand", (e) => Ac(e, !0, !0, !0)), Q("\\message", (e) => {
	var t = e.consumeArgs(1)[0];
	return console.log(t.reverse().map((e) => e.text).join("")), "";
}), Q("\\errmessage", (e) => {
	var t = e.consumeArgs(1)[0];
	return console.error(t.reverse().map((e) => e.text).join("")), "";
}), Q("\\show", (e) => {
	var t = e.popToken(), n = t.text;
	return console.log(t, e.macros.get(n), _c[n], pi.math[n], pi.text[n]), "";
}), Q("\\bgroup", "{"), Q("\\egroup", "}"), Q("~", "\\nobreakspace"), Q("\\lq", "`"), Q("\\rq", "'"), Q("\\aa", "\\r a"), Q("\\AA", "\\r A"), Q("\\textcopyright", "\\html@mathml{\\textcircled{c}}{\\char`©}"), Q("\\copyright", "\\TextOrMath{\\textcopyright}{\\text{\\textcopyright}}"), Q("\\textregistered", "\\html@mathml{\\textcircled{\\scriptsize R}}{\\char`®}"), Q("ℬ", "\\mathscr{B}"), Q("ℰ", "\\mathscr{E}"), Q("ℱ", "\\mathscr{F}"), Q("ℋ", "\\mathscr{H}"), Q("ℐ", "\\mathscr{I}"), Q("ℒ", "\\mathscr{L}"), Q("ℳ", "\\mathscr{M}"), Q("ℛ", "\\mathscr{R}"), Q("ℭ", "\\mathfrak{C}"), Q("ℌ", "\\mathfrak{H}"), Q("ℨ", "\\mathfrak{Z}"), Q("\\Bbbk", "\\Bbb{k}"), Q("\\llap", "\\mathllap{\\textrm{#1}}"), Q("\\rlap", "\\mathrlap{\\textrm{#1}}"), Q("\\clap", "\\mathclap{\\textrm{#1}}"), Q("\\mathstrut", "\\vphantom{(}"), Q("\\underbar", "\\underline{\\text{#1}}"), Q("\\not", "\\html@mathml{\\mathrel{\\mathrlap\\@not}\\nobreak}{\\char\"338}"), Q("\\neq", "\\html@mathml{\\mathrel{\\not=}}{\\mathrel{\\char`≠}}"), Q("\\ne", "\\neq"), Q("≠", "\\neq"), Q("\\notin", "\\html@mathml{\\mathrel{{\\in}\\mathllap{/\\mskip1mu}}}{\\mathrel{\\char`∉}}"), Q("∉", "\\notin"), Q("≘", "\\html@mathml{\\mathrel{=\\kern{-1em}\\raisebox{0.4em}{$\\scriptsize\\frown$}}}{\\mathrel{\\char`≘}}"), Q("≙", "\\html@mathml{\\stackrel{\\tiny\\wedge}{=}}{\\mathrel{\\char`≘}}"), Q("≚", "\\html@mathml{\\stackrel{\\tiny\\vee}{=}}{\\mathrel{\\char`≚}}"), Q("≛", "\\html@mathml{\\stackrel{\\scriptsize\\star}{=}}{\\mathrel{\\char`≛}}"), Q("≝", "\\html@mathml{\\stackrel{\\tiny\\mathrm{def}}{=}}{\\mathrel{\\char`≝}}"), Q("≞", "\\html@mathml{\\stackrel{\\tiny\\mathrm{m}}{=}}{\\mathrel{\\char`≞}}"), Q("≟", "\\html@mathml{\\stackrel{\\tiny?}{=}}{\\mathrel{\\char`≟}}"), Q("⟂", "\\perp"), Q("‼", "\\mathclose{!\\mkern-0.8mu!}"), Q("∌", "\\notni"), Q("⌜", "\\ulcorner"), Q("⌝", "\\urcorner"), Q("⌞", "\\llcorner"), Q("⌟", "\\lrcorner"), Q("©", "\\copyright"), Q("®", "\\textregistered"), Q("\\ulcorner", "\\html@mathml{\\@ulcorner}{\\mathop{\\char\"231c}}"), Q("\\urcorner", "\\html@mathml{\\@urcorner}{\\mathop{\\char\"231d}}"), Q("\\llcorner", "\\html@mathml{\\@llcorner}{\\mathop{\\char\"231e}}"), Q("\\lrcorner", "\\html@mathml{\\@lrcorner}{\\mathop{\\char\"231f}}"), Q("\\vdots", "{\\varvdots\\rule{0pt}{15pt}}"), Q("⋮", "\\vdots"), Q("\\varGamma", "\\mathit{\\Gamma}"), Q("\\varDelta", "\\mathit{\\Delta}"), Q("\\varTheta", "\\mathit{\\Theta}"), Q("\\varLambda", "\\mathit{\\Lambda}"), Q("\\varXi", "\\mathit{\\Xi}"), Q("\\varPi", "\\mathit{\\Pi}"), Q("\\varSigma", "\\mathit{\\Sigma}"), Q("\\varUpsilon", "\\mathit{\\Upsilon}"), Q("\\varPhi", "\\mathit{\\Phi}"), Q("\\varPsi", "\\mathit{\\Psi}"), Q("\\varOmega", "\\mathit{\\Omega}"), Q("\\substack", "\\begin{subarray}{c}#1\\end{subarray}"), Q("\\colon", "\\nobreak\\mskip2mu\\mathpunct{}\\mathchoice{\\mkern-3mu}{\\mkern-3mu}{}{}{:}\\mskip6mu\\relax"), Q("\\boxed", "\\fbox{$\\displaystyle{#1}$}"), Q("\\iff", "\\DOTSB\\;\\Longleftrightarrow\\;"), Q("\\implies", "\\DOTSB\\;\\Longrightarrow\\;"), Q("\\impliedby", "\\DOTSB\\;\\Longleftarrow\\;"), Q("\\dddot", "{\\overset{\\raisebox{-0.1ex}{\\normalsize ...}}{#1}}"), Q("\\ddddot", "{\\overset{\\raisebox{-0.1ex}{\\normalsize ....}}{#1}}");
var jc = {
	",": "\\dotsc",
	"\\not": "\\dotsb",
	"+": "\\dotsb",
	"=": "\\dotsb",
	"<": "\\dotsb",
	">": "\\dotsb",
	"-": "\\dotsb",
	"*": "\\dotsb",
	":": "\\dotsb",
	"\\DOTSB": "\\dotsb",
	"\\coprod": "\\dotsb",
	"\\bigvee": "\\dotsb",
	"\\bigwedge": "\\dotsb",
	"\\biguplus": "\\dotsb",
	"\\bigcap": "\\dotsb",
	"\\bigcup": "\\dotsb",
	"\\prod": "\\dotsb",
	"\\sum": "\\dotsb",
	"\\bigotimes": "\\dotsb",
	"\\bigoplus": "\\dotsb",
	"\\bigodot": "\\dotsb",
	"\\bigsqcup": "\\dotsb",
	"\\And": "\\dotsb",
	"\\longrightarrow": "\\dotsb",
	"\\Longrightarrow": "\\dotsb",
	"\\longleftarrow": "\\dotsb",
	"\\Longleftarrow": "\\dotsb",
	"\\longleftrightarrow": "\\dotsb",
	"\\Longleftrightarrow": "\\dotsb",
	"\\mapsto": "\\dotsb",
	"\\longmapsto": "\\dotsb",
	"\\hookrightarrow": "\\dotsb",
	"\\doteq": "\\dotsb",
	"\\mathbin": "\\dotsb",
	"\\mathrel": "\\dotsb",
	"\\relbar": "\\dotsb",
	"\\Relbar": "\\dotsb",
	"\\xrightarrow": "\\dotsb",
	"\\xleftarrow": "\\dotsb",
	"\\DOTSI": "\\dotsi",
	"\\int": "\\dotsi",
	"\\oint": "\\dotsi",
	"\\iint": "\\dotsi",
	"\\iiint": "\\dotsi",
	"\\iiiint": "\\dotsi",
	"\\idotsint": "\\dotsi",
	"\\DOTSX": "\\dotsx"
}, Mc = new Set(["bin", "rel"]);
Q("\\dots", function(e) {
	var t = "\\dotso", n = e.expandAfterFuture().text;
	return n in jc ? t = jc[n] : (n.slice(0, 4) === "\\not" || n in pi.math && Mc.has(pi.math[n].group)) && (t = "\\dotsb"), t;
});
var Nc = {
	")": !0,
	"]": !0,
	"\\rbrack": !0,
	"\\}": !0,
	"\\rbrace": !0,
	"\\rangle": !0,
	"\\rceil": !0,
	"\\rfloor": !0,
	"\\rgroup": !0,
	"\\rmoustache": !0,
	"\\right": !0,
	"\\bigr": !0,
	"\\biggr": !0,
	"\\Bigr": !0,
	"\\Biggr": !0,
	$: !0,
	";": !0,
	".": !0,
	",": !0
};
Q("\\dotso", function(e) {
	return e.future().text in Nc ? "\\ldots\\," : "\\ldots";
}), Q("\\dotsc", function(e) {
	var t = e.future().text;
	return t in Nc && t !== "," ? "\\ldots\\," : "\\ldots";
}), Q("\\cdots", function(e) {
	return e.future().text in Nc ? "\\@cdots\\," : "\\@cdots";
}), Q("\\dotsb", "\\cdots"), Q("\\dotsm", "\\cdots"), Q("\\dotsi", "\\!\\cdots"), Q("\\dotsx", "\\ldots\\,"), Q("\\DOTSI", "\\relax"), Q("\\DOTSB", "\\relax"), Q("\\DOTSX", "\\relax"), Q("\\tmspace", "\\TextOrMath{\\kern#1#3}{\\mskip#1#2}\\relax"), Q("\\,", "\\tmspace+{3mu}{.1667em}"), Q("\\thinspace", "\\,"), Q("\\>", "\\mskip{4mu}"), Q("\\:", "\\tmspace+{4mu}{.2222em}"), Q("\\medspace", "\\:"), Q("\\;", "\\tmspace+{5mu}{.2777em}"), Q("\\thickspace", "\\;"), Q("\\!", "\\tmspace-{3mu}{.1667em}"), Q("\\negthinspace", "\\!"), Q("\\negmedspace", "\\tmspace-{4mu}{.2222em}"), Q("\\negthickspace", "\\tmspace-{5mu}{.277em}"), Q("\\enspace", "\\kern.5em "), Q("\\enskip", "\\hskip.5em\\relax"), Q("\\quad", "\\hskip1em\\relax"), Q("\\qquad", "\\hskip2em\\relax"), Q("\\tag", "\\@ifstar\\tag@literal\\tag@paren"), Q("\\tag@paren", "\\tag@literal{({#1})}"), Q("\\tag@literal", (e) => {
	if (e.macros.get("\\df@tag")) throw new N("Multiple \\tag");
	return "\\gdef\\df@tag{\\text{#1}}";
}), Q("\\bmod", "\\mathchoice{\\mskip1mu}{\\mskip1mu}{\\mskip5mu}{\\mskip5mu}\\mathbin{\\rm mod}\\mathchoice{\\mskip1mu}{\\mskip1mu}{\\mskip5mu}{\\mskip5mu}"), Q("\\pod", "\\allowbreak\\mathchoice{\\mkern18mu}{\\mkern8mu}{\\mkern8mu}{\\mkern8mu}(#1)"), Q("\\pmod", "\\pod{{\\rm mod}\\mkern6mu#1}"), Q("\\mod", "\\allowbreak\\mathchoice{\\mkern18mu}{\\mkern12mu}{\\mkern12mu}{\\mkern12mu}{\\rm mod}\\,\\,#1"), Q("\\newline", "\\\\\\relax"), Q("\\TeX", "\\textrm{\\html@mathml{T\\kern-.1667em\\raisebox{-.5ex}{E}\\kern-.125emX}{TeX}}");
var Pc = F(oi["Main-Regular"][84][1] - .7 * oi["Main-Regular"][65][1]);
Q("\\LaTeX", "\\textrm{\\html@mathml{" + ("L\\kern-.36em\\raisebox{" + Pc + "}{\\scriptstyle A}") + "\\kern-.15em\\TeX}{LaTeX}}"), Q("\\KaTeX", "\\textrm{\\html@mathml{" + ("K\\kern-.17em\\raisebox{" + Pc + "}{\\scriptstyle A}") + "\\kern-.15em\\TeX}{KaTeX}}"), Q("\\hspace", "\\@ifstar\\@hspacer\\@hspace"), Q("\\@hspace", "\\hskip #1\\relax"), Q("\\@hspacer", "\\rule{0pt}{0pt}\\hskip #1\\relax"), Q("\\ordinarycolon", ":"), Q("\\vcentcolon", "\\mathrel{\\mathop\\ordinarycolon}"), Q("\\dblcolon", "\\html@mathml{\\mathrel{\\vcentcolon\\mathrel{\\mkern-.9mu}\\vcentcolon}}{\\mathop{\\char\"2237}}"), Q("\\coloneqq", "\\html@mathml{\\mathrel{\\vcentcolon\\mathrel{\\mkern-1.2mu}=}}{\\mathop{\\char\"2254}}"), Q("\\Coloneqq", "\\html@mathml{\\mathrel{\\dblcolon\\mathrel{\\mkern-1.2mu}=}}{\\mathop{\\char\"2237\\char\"3d}}"), Q("\\coloneq", "\\html@mathml{\\mathrel{\\vcentcolon\\mathrel{\\mkern-1.2mu}\\mathrel{-}}}{\\mathop{\\char\"3a\\char\"2212}}"), Q("\\Coloneq", "\\html@mathml{\\mathrel{\\dblcolon\\mathrel{\\mkern-1.2mu}\\mathrel{-}}}{\\mathop{\\char\"2237\\char\"2212}}"), Q("\\eqqcolon", "\\html@mathml{\\mathrel{=\\mathrel{\\mkern-1.2mu}\\vcentcolon}}{\\mathop{\\char\"2255}}"), Q("\\Eqqcolon", "\\html@mathml{\\mathrel{=\\mathrel{\\mkern-1.2mu}\\dblcolon}}{\\mathop{\\char\"3d\\char\"2237}}"), Q("\\eqcolon", "\\html@mathml{\\mathrel{\\mathrel{-}\\mathrel{\\mkern-1.2mu}\\vcentcolon}}{\\mathop{\\char\"2239}}"), Q("\\Eqcolon", "\\html@mathml{\\mathrel{\\mathrel{-}\\mathrel{\\mkern-1.2mu}\\dblcolon}}{\\mathop{\\char\"2212\\char\"2237}}"), Q("\\colonapprox", "\\html@mathml{\\mathrel{\\vcentcolon\\mathrel{\\mkern-1.2mu}\\approx}}{\\mathop{\\char\"3a\\char\"2248}}"), Q("\\Colonapprox", "\\html@mathml{\\mathrel{\\dblcolon\\mathrel{\\mkern-1.2mu}\\approx}}{\\mathop{\\char\"2237\\char\"2248}}"), Q("\\colonsim", "\\html@mathml{\\mathrel{\\vcentcolon\\mathrel{\\mkern-1.2mu}\\sim}}{\\mathop{\\char\"3a\\char\"223c}}"), Q("\\Colonsim", "\\html@mathml{\\mathrel{\\dblcolon\\mathrel{\\mkern-1.2mu}\\sim}}{\\mathop{\\char\"2237\\char\"223c}}"), Q("∷", "\\dblcolon"), Q("∹", "\\eqcolon"), Q("≔", "\\coloneqq"), Q("≕", "\\eqqcolon"), Q("⩴", "\\Coloneqq"), Q("\\ratio", "\\vcentcolon"), Q("\\coloncolon", "\\dblcolon"), Q("\\colonequals", "\\coloneqq"), Q("\\coloncolonequals", "\\Coloneqq"), Q("\\equalscolon", "\\eqqcolon"), Q("\\equalscoloncolon", "\\Eqqcolon"), Q("\\colonminus", "\\coloneq"), Q("\\coloncolonminus", "\\Coloneq"), Q("\\minuscolon", "\\eqcolon"), Q("\\minuscoloncolon", "\\Eqcolon"), Q("\\coloncolonapprox", "\\Colonapprox"), Q("\\coloncolonsim", "\\Colonsim"), Q("\\simcolon", "\\mathrel{\\sim\\mathrel{\\mkern-1.2mu}\\vcentcolon}"), Q("\\simcoloncolon", "\\mathrel{\\sim\\mathrel{\\mkern-1.2mu}\\dblcolon}"), Q("\\approxcolon", "\\mathrel{\\approx\\mathrel{\\mkern-1.2mu}\\vcentcolon}"), Q("\\approxcoloncolon", "\\mathrel{\\approx\\mathrel{\\mkern-1.2mu}\\dblcolon}"), Q("\\notni", "\\html@mathml{\\not\\ni}{\\mathrel{\\char`∌}}"), Q("\\limsup", "\\DOTSB\\operatorname*{lim\\,sup}"), Q("\\liminf", "\\DOTSB\\operatorname*{lim\\,inf}"), Q("\\injlim", "\\DOTSB\\operatorname*{inj\\,lim}"), Q("\\projlim", "\\DOTSB\\operatorname*{proj\\,lim}"), Q("\\varlimsup", "\\DOTSB\\operatorname*{\\overline{lim}}"), Q("\\varliminf", "\\DOTSB\\operatorname*{\\underline{lim}}"), Q("\\varinjlim", "\\DOTSB\\operatorname*{\\underrightarrow{lim}}"), Q("\\varprojlim", "\\DOTSB\\operatorname*{\\underleftarrow{lim}}"), Q("\\gvertneqq", "\\html@mathml{\\@gvertneqq}{≩}"), Q("\\lvertneqq", "\\html@mathml{\\@lvertneqq}{≨}"), Q("\\ngeqq", "\\html@mathml{\\@ngeqq}{≱}"), Q("\\ngeqslant", "\\html@mathml{\\@ngeqslant}{≱}"), Q("\\nleqq", "\\html@mathml{\\@nleqq}{≰}"), Q("\\nleqslant", "\\html@mathml{\\@nleqslant}{≰}"), Q("\\nshortmid", "\\html@mathml{\\@nshortmid}{∤}"), Q("\\nshortparallel", "\\html@mathml{\\@nshortparallel}{∦}"), Q("\\nsubseteqq", "\\html@mathml{\\@nsubseteqq}{⊈}"), Q("\\nsupseteqq", "\\html@mathml{\\@nsupseteqq}{⊉}"), Q("\\varsubsetneq", "\\html@mathml{\\@varsubsetneq}{⊊}"), Q("\\varsubsetneqq", "\\html@mathml{\\@varsubsetneqq}{⫋}"), Q("\\varsupsetneq", "\\html@mathml{\\@varsupsetneq}{⊋}"), Q("\\varsupsetneqq", "\\html@mathml{\\@varsupsetneqq}{⫌}"), Q("\\imath", "\\html@mathml{\\@imath}{ı}"), Q("\\jmath", "\\html@mathml{\\@jmath}{ȷ}"), Q("\\llbracket", "\\html@mathml{\\mathopen{[\\mkern-3.2mu[}}{\\mathopen{\\char`⟦}}"), Q("\\rrbracket", "\\html@mathml{\\mathclose{]\\mkern-3.2mu]}}{\\mathclose{\\char`⟧}}"), Q("⟦", "\\llbracket"), Q("⟧", "\\rrbracket"), Q("\\lBrace", "\\html@mathml{\\mathopen{\\{\\mkern-3.2mu[}}{\\mathopen{\\char`⦃}}"), Q("\\rBrace", "\\html@mathml{\\mathclose{]\\mkern-3.2mu\\}}}{\\mathclose{\\char`⦄}}"), Q("⦃", "\\lBrace"), Q("⦄", "\\rBrace"), Q("\\minuso", "\\mathbin{\\html@mathml{{\\mathrlap{\\mathchoice{\\kern{0.145em}}{\\kern{0.145em}}{\\kern{0.1015em}}{\\kern{0.0725em}}\\circ}{-}}}{\\char`⦵}}"), Q("⦵", "\\minuso"), Q("\\darr", "\\downarrow"), Q("\\dArr", "\\Downarrow"), Q("\\Darr", "\\Downarrow"), Q("\\lang", "\\langle"), Q("\\rang", "\\rangle"), Q("\\uarr", "\\uparrow"), Q("\\uArr", "\\Uparrow"), Q("\\Uarr", "\\Uparrow"), Q("\\N", "\\mathbb{N}"), Q("\\R", "\\mathbb{R}"), Q("\\Z", "\\mathbb{Z}"), Q("\\alef", "\\aleph"), Q("\\alefsym", "\\aleph"), Q("\\Alpha", "\\mathrm{A}"), Q("\\Beta", "\\mathrm{B}"), Q("\\bull", "\\bullet"), Q("\\Chi", "\\mathrm{X}"), Q("\\clubs", "\\clubsuit"), Q("\\cnums", "\\mathbb{C}"), Q("\\Complex", "\\mathbb{C}"), Q("\\Dagger", "\\ddagger"), Q("\\diamonds", "\\diamondsuit"), Q("\\empty", "\\emptyset"), Q("\\Epsilon", "\\mathrm{E}"), Q("\\Eta", "\\mathrm{H}"), Q("\\exist", "\\exists"), Q("\\harr", "\\leftrightarrow"), Q("\\hArr", "\\Leftrightarrow"), Q("\\Harr", "\\Leftrightarrow"), Q("\\hearts", "\\heartsuit"), Q("\\image", "\\Im"), Q("\\infin", "\\infty"), Q("\\Iota", "\\mathrm{I}"), Q("\\isin", "\\in"), Q("\\Kappa", "\\mathrm{K}"), Q("\\larr", "\\leftarrow"), Q("\\lArr", "\\Leftarrow"), Q("\\Larr", "\\Leftarrow"), Q("\\lrarr", "\\leftrightarrow"), Q("\\lrArr", "\\Leftrightarrow"), Q("\\Lrarr", "\\Leftrightarrow"), Q("\\Mu", "\\mathrm{M}"), Q("\\natnums", "\\mathbb{N}"), Q("\\Nu", "\\mathrm{N}"), Q("\\Omicron", "\\mathrm{O}"), Q("\\plusmn", "\\pm"), Q("\\rarr", "\\rightarrow"), Q("\\rArr", "\\Rightarrow"), Q("\\Rarr", "\\Rightarrow"), Q("\\real", "\\Re"), Q("\\reals", "\\mathbb{R}"), Q("\\Reals", "\\mathbb{R}"), Q("\\Rho", "\\mathrm{P}"), Q("\\sdot", "\\cdot"), Q("\\sect", "\\S"), Q("\\spades", "\\spadesuit"), Q("\\sub", "\\subset"), Q("\\sube", "\\subseteq"), Q("\\supe", "\\supseteq"), Q("\\Tau", "\\mathrm{T}"), Q("\\thetasym", "\\vartheta"), Q("\\weierp", "\\wp"), Q("\\Zeta", "\\mathrm{Z}"), Q("\\argmin", "\\DOTSB\\operatorname*{arg\\,min}"), Q("\\argmax", "\\DOTSB\\operatorname*{arg\\,max}"), Q("\\plim", "\\DOTSB\\mathop{\\operatorname{plim}}\\limits"), Q("\\bra", "\\mathinner{\\langle{#1}|}"), Q("\\ket", "\\mathinner{|{#1}\\rangle}"), Q("\\braket", "\\mathinner{\\langle{#1}\\rangle}"), Q("\\Bra", "\\left\\langle#1\\right|"), Q("\\Ket", "\\left|#1\\right\\rangle");
var Fc = (e) => (t) => {
	var n = t.consumeArg().tokens, r = t.consumeArg().tokens, i = t.consumeArg().tokens, a = t.consumeArg().tokens, o = t.macros.get("|"), s = t.macros.get("\\|");
	t.macros.beginGroup();
	var c = (t) => (n) => {
		e && (n.macros.set("|", o), i.length && n.macros.set("\\|", s));
		var a = t;
		return !t && i.length && n.future().text === "|" && (n.popToken(), a = !0), {
			tokens: a ? i : r,
			numArgs: 0
		};
	};
	t.macros.set("|", c(!1)), i.length && t.macros.set("\\|", c(!0));
	var l = t.consumeArg().tokens, u = t.expandTokens([
		...a,
		...l,
		...n
	]);
	return t.macros.endGroup(), {
		tokens: u.reverse(),
		numArgs: 0
	};
};
Q("\\bra@ket", Fc(!1)), Q("\\bra@set", Fc(!0)), Q("\\Braket", "\\bra@ket{\\left\\langle}{\\,\\middle\\vert\\,}{\\,\\middle\\vert\\,}{\\right\\rangle}"), Q("\\Set", "\\bra@set{\\left\\{\\:}{\\;\\middle\\vert\\;}{\\;\\middle\\Vert\\;}{\\:\\right\\}}"), Q("\\set", "\\bra@set{\\{\\,}{\\mid}{}{\\,\\}}"), Q("\\angln", "{\\angl n}"), Q("\\blue", "\\textcolor{##6495ed}{#1}"), Q("\\orange", "\\textcolor{##ffa500}{#1}"), Q("\\pink", "\\textcolor{##ff00af}{#1}"), Q("\\red", "\\textcolor{##df0030}{#1}"), Q("\\green", "\\textcolor{##28ae7b}{#1}"), Q("\\gray", "\\textcolor{gray}{#1}"), Q("\\purple", "\\textcolor{##9d38bd}{#1}"), Q("\\blueA", "\\textcolor{##ccfaff}{#1}"), Q("\\blueB", "\\textcolor{##80f6ff}{#1}"), Q("\\blueC", "\\textcolor{##63d9ea}{#1}"), Q("\\blueD", "\\textcolor{##11accd}{#1}"), Q("\\blueE", "\\textcolor{##0c7f99}{#1}"), Q("\\tealA", "\\textcolor{##94fff5}{#1}"), Q("\\tealB", "\\textcolor{##26edd5}{#1}"), Q("\\tealC", "\\textcolor{##01d1c1}{#1}"), Q("\\tealD", "\\textcolor{##01a995}{#1}"), Q("\\tealE", "\\textcolor{##208170}{#1}"), Q("\\greenA", "\\textcolor{##b6ffb0}{#1}"), Q("\\greenB", "\\textcolor{##8af281}{#1}"), Q("\\greenC", "\\textcolor{##74cf70}{#1}"), Q("\\greenD", "\\textcolor{##1fab54}{#1}"), Q("\\greenE", "\\textcolor{##0d923f}{#1}"), Q("\\goldA", "\\textcolor{##ffd0a9}{#1}"), Q("\\goldB", "\\textcolor{##ffbb71}{#1}"), Q("\\goldC", "\\textcolor{##ff9c39}{#1}"), Q("\\goldD", "\\textcolor{##e07d10}{#1}"), Q("\\goldE", "\\textcolor{##a75a05}{#1}"), Q("\\redA", "\\textcolor{##fca9a9}{#1}"), Q("\\redB", "\\textcolor{##ff8482}{#1}"), Q("\\redC", "\\textcolor{##f9685d}{#1}"), Q("\\redD", "\\textcolor{##e84d39}{#1}"), Q("\\redE", "\\textcolor{##bc2612}{#1}"), Q("\\maroonA", "\\textcolor{##ffbde0}{#1}"), Q("\\maroonB", "\\textcolor{##ff92c6}{#1}"), Q("\\maroonC", "\\textcolor{##ed5fa6}{#1}"), Q("\\maroonD", "\\textcolor{##ca337c}{#1}"), Q("\\maroonE", "\\textcolor{##9e034e}{#1}"), Q("\\purpleA", "\\textcolor{##ddd7ff}{#1}"), Q("\\purpleB", "\\textcolor{##c6b9fc}{#1}"), Q("\\purpleC", "\\textcolor{##aa87ff}{#1}"), Q("\\purpleD", "\\textcolor{##7854ab}{#1}"), Q("\\purpleE", "\\textcolor{##543b78}{#1}"), Q("\\mintA", "\\textcolor{##f5f9e8}{#1}"), Q("\\mintB", "\\textcolor{##edf2df}{#1}"), Q("\\mintC", "\\textcolor{##e0e5cc}{#1}"), Q("\\grayA", "\\textcolor{##f6f7f7}{#1}"), Q("\\grayB", "\\textcolor{##f0f1f2}{#1}"), Q("\\grayC", "\\textcolor{##e3e5e6}{#1}"), Q("\\grayD", "\\textcolor{##d6d8da}{#1}"), Q("\\grayE", "\\textcolor{##babec2}{#1}"), Q("\\grayF", "\\textcolor{##888d93}{#1}"), Q("\\grayG", "\\textcolor{##626569}{#1}"), Q("\\grayH", "\\textcolor{##3b3e40}{#1}"), Q("\\grayI", "\\textcolor{##21242c}{#1}"), Q("\\kaBlue", "\\textcolor{##314453}{#1}"), Q("\\kaGreen", "\\textcolor{##71B307}{#1}");
var Ic = {
	"^": !0,
	_: !0,
	"\\limits": !0,
	"\\nolimits": !0
}, Lc = class {
	constructor(e, t, n) {
		this.settings = void 0, this.expansionCount = void 0, this.lexer = void 0, this.macros = void 0, this.stack = void 0, this.mode = void 0, this.settings = t, this.expansionCount = 0, this.feed(e), this.macros = new Dc(Oc, t.macros), this.mode = n, this.stack = [];
	}
	feed(e) {
		this.lexer = new Ec(e, this.settings);
	}
	switchMode(e) {
		this.mode = e;
	}
	beginGroup() {
		this.macros.beginGroup();
	}
	endGroup() {
		this.macros.endGroup();
	}
	endGroups() {
		this.macros.endGroups();
	}
	future() {
		return this.stack.length === 0 && this.pushToken(this.lexer.lex()), this.stack[this.stack.length - 1];
	}
	popToken() {
		return this.future(), this.stack.pop();
	}
	pushToken(e) {
		this.stack.push(e);
	}
	pushTokens(e) {
		this.stack.push(...e);
	}
	scanArgument(e) {
		var t, n, r;
		if (e) {
			if (this.consumeSpaces(), this.future().text !== "[") return null;
			t = this.popToken(), {tokens: r, end: n} = this.consumeArg(["]"]);
		} else ({tokens: r, start: t, end: n} = this.consumeArg());
		return this.pushToken(new Ds("EOF", n.loc)), this.pushTokens(r), new Ds("", Es.range(t, n));
	}
	consumeSpaces() {
		for (; this.future().text === " ";) this.stack.pop();
	}
	consumeArg(e) {
		var t = [], n = e && e.length > 0;
		n || this.consumeSpaces();
		var r = this.future(), i, a = 0, o = 0;
		do {
			if (i = this.popToken(), t.push(i), i.text === "{") ++a;
			else if (i.text === "}") {
				if (--a, a === -1) throw new N("Extra }", i);
			} else if (i.text === "EOF") throw new N("Unexpected end of input in a macro argument, expected '" + (e && n ? e[o] : "}") + "'", i);
			if (e && n) if ((a === 0 || a === 1 && e[o] === "{") && i.text === e[o]) {
				if (++o, o === e.length) {
					t.splice(-o, o);
					break;
				}
			} else o = 0;
		} while (a !== 0 || n);
		return r.text === "{" && t[t.length - 1].text === "}" && (t.pop(), t.shift()), t.reverse(), {
			tokens: t,
			start: r,
			end: i
		};
	}
	consumeArgs(e, t) {
		if (t) {
			if (t.length !== e + 1) throw new N("The length of delimiters doesn't match the number of args!");
			for (var n = t[0], r = 0; r < n.length; r++) {
				var i = this.popToken();
				if (n[r] !== i.text) throw new N("Use of the macro doesn't match its definition", i);
			}
		}
		for (var a = [], o = 0; o < e; o++) a.push(this.consumeArg(t && t[o + 1]).tokens);
		return a;
	}
	countExpansion(e) {
		if (this.expansionCount += e, this.expansionCount > this.settings.maxExpand) throw new N("Too many expansions: infinite loop or need to increase maxExpand setting");
	}
	expandOnce(e) {
		var t = this.popToken(), n = t.text, r = t.noexpand ? null : this._getExpansion(n);
		if (r == null || e && r.unexpandable) {
			if (e && r == null && n[0] === "\\" && !this.isDefined(n)) throw new N("Undefined control sequence: " + n);
			return this.pushToken(t), !1;
		}
		this.countExpansion(1);
		var i = r.tokens, a = this.consumeArgs(r.numArgs, r.delimiters);
		if (r.numArgs) {
			i = i.slice();
			for (var o = i.length - 1; o >= 0; --o) {
				var s = i[o];
				if (s.text === "#") {
					if (o === 0) throw new N("Incomplete placeholder at end of macro body", s);
					if (s = i[--o], s.text === "#") i.splice(o + 1, 1);
					else if (/^[1-9]$/.test(s.text)) i.splice(o, 2, ...a[s.text - 1]);
					else throw new N("Not a valid argument number", s);
				}
			}
		}
		return this.pushTokens(i), i.length;
	}
	expandAfterFuture() {
		return this.expandOnce(), this.future();
	}
	expandNextToken() {
		for (;;) if (this.expandOnce() === !1) {
			var e = this.stack.pop();
			return e.treatAsRelax && (e.text = "\\relax"), e;
		}
	}
	expandMacro(e) {
		return this.macros.has(e) ? this.expandTokens([new Ds(e)]) : void 0;
	}
	expandTokens(e) {
		var t = [], n = this.stack.length;
		for (this.pushTokens(e); this.stack.length > n;) if (this.expandOnce(!0) === !1) {
			var r = this.stack.pop();
			r.treatAsRelax &&= (r.noexpand = !1, !1), t.push(r);
		}
		return this.countExpansion(t.length), t;
	}
	expandMacroAsText(e) {
		var t = this.expandMacro(e);
		return t && t.map((e) => e.text).join("");
	}
	_getExpansion(e) {
		var t = this.macros.get(e);
		if (t == null) return t;
		if (e.length === 1) {
			var n = this.lexer.catcodes[e];
			if (n != null && n !== 13) return;
		}
		var r = typeof t == "function" ? t(this) : t;
		if (typeof r == "string") {
			var i = 0;
			if (r.includes("#")) for (var a = r.replace(/##/g, ""); a.includes("#" + (i + 1));) ++i;
			for (var o = new Ec(r, this.settings), s = [], c = o.lex(); c.text !== "EOF";) s.push(c), c = o.lex();
			return s.reverse(), {
				tokens: s,
				numArgs: i
			};
		}
		return r;
	}
	isDefined(e) {
		return this.macros.has(e) || _c.hasOwnProperty(e) || pi.math.hasOwnProperty(e) || pi.text.hasOwnProperty(e) || Ic.hasOwnProperty(e);
	}
	isExpandable(e) {
		var t = this.macros.get(e);
		return t == null ? _c.hasOwnProperty(e) && !_c[e].primitive : typeof t == "string" || typeof t == "function" || !t.unexpandable;
	}
}, Rc = /^[₊₋₌₍₎₀₁₂₃₄₅₆₇₈₉ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵪ]/, zc = Object.freeze({
	"₊": "+",
	"₋": "-",
	"₌": "=",
	"₍": "(",
	"₎": ")",
	"₀": "0",
	"₁": "1",
	"₂": "2",
	"₃": "3",
	"₄": "4",
	"₅": "5",
	"₆": "6",
	"₇": "7",
	"₈": "8",
	"₉": "9",
	ₐ: "a",
	ₑ: "e",
	ₕ: "h",
	ᵢ: "i",
	ⱼ: "j",
	ₖ: "k",
	ₗ: "l",
	ₘ: "m",
	ₙ: "n",
	ₒ: "o",
	ₚ: "p",
	ᵣ: "r",
	ₛ: "s",
	ₜ: "t",
	ᵤ: "u",
	ᵥ: "v",
	ₓ: "x",
	ᵦ: "β",
	ᵧ: "γ",
	ᵨ: "ρ",
	ᵩ: "ϕ",
	ᵪ: "χ",
	"⁺": "+",
	"⁻": "-",
	"⁼": "=",
	"⁽": "(",
	"⁾": ")",
	"⁰": "0",
	"¹": "1",
	"²": "2",
	"³": "3",
	"⁴": "4",
	"⁵": "5",
	"⁶": "6",
	"⁷": "7",
	"⁸": "8",
	"⁹": "9",
	ᴬ: "A",
	ᴮ: "B",
	ᴰ: "D",
	ᴱ: "E",
	ᴳ: "G",
	ᴴ: "H",
	ᴵ: "I",
	ᴶ: "J",
	ᴷ: "K",
	ᴸ: "L",
	ᴹ: "M",
	ᴺ: "N",
	ᴼ: "O",
	ᴾ: "P",
	ᴿ: "R",
	ᵀ: "T",
	ᵁ: "U",
	ⱽ: "V",
	ᵂ: "W",
	ᵃ: "a",
	ᵇ: "b",
	ᶜ: "c",
	ᵈ: "d",
	ᵉ: "e",
	ᶠ: "f",
	ᵍ: "g",
	ʰ: "h",
	ⁱ: "i",
	ʲ: "j",
	ᵏ: "k",
	ˡ: "l",
	ᵐ: "m",
	ⁿ: "n",
	ᵒ: "o",
	ᵖ: "p",
	ʳ: "r",
	ˢ: "s",
	ᵗ: "t",
	ᵘ: "u",
	ᵛ: "v",
	ʷ: "w",
	ˣ: "x",
	ʸ: "y",
	ᶻ: "z",
	ᵝ: "β",
	ᵞ: "γ",
	ᵟ: "δ",
	ᵠ: "ϕ",
	ᵡ: "χ",
	ᶿ: "θ"
}), Bc = {
	"́": {
		text: "\\'",
		math: "\\acute"
	},
	"̀": {
		text: "\\`",
		math: "\\grave"
	},
	"̈": {
		text: "\\\"",
		math: "\\ddot"
	},
	"̃": {
		text: "\\~",
		math: "\\tilde"
	},
	"̄": {
		text: "\\=",
		math: "\\bar"
	},
	"̆": {
		text: "\\u",
		math: "\\breve"
	},
	"̌": {
		text: "\\v",
		math: "\\check"
	},
	"̂": {
		text: "\\^",
		math: "\\hat"
	},
	"̇": {
		text: "\\.",
		math: "\\dot"
	},
	"̊": {
		text: "\\r",
		math: "\\mathring"
	},
	"̋": { text: "\\H" },
	"̧": { text: "\\c" }
}, Vc = {
	á: "á",
	à: "à",
	ä: "ä",
	ǟ: "ǟ",
	ã: "ã",
	ā: "ā",
	ă: "ă",
	ắ: "ắ",
	ằ: "ằ",
	ẵ: "ẵ",
	ǎ: "ǎ",
	â: "â",
	ấ: "ấ",
	ầ: "ầ",
	ẫ: "ẫ",
	ȧ: "ȧ",
	ǡ: "ǡ",
	å: "å",
	ǻ: "ǻ",
	ḃ: "ḃ",
	ć: "ć",
	ḉ: "ḉ",
	č: "č",
	ĉ: "ĉ",
	ċ: "ċ",
	ç: "ç",
	ď: "ď",
	ḋ: "ḋ",
	ḑ: "ḑ",
	é: "é",
	è: "è",
	ë: "ë",
	ẽ: "ẽ",
	ē: "ē",
	ḗ: "ḗ",
	ḕ: "ḕ",
	ĕ: "ĕ",
	ḝ: "ḝ",
	ě: "ě",
	ê: "ê",
	ế: "ế",
	ề: "ề",
	ễ: "ễ",
	ė: "ė",
	ȩ: "ȩ",
	ḟ: "ḟ",
	ǵ: "ǵ",
	ḡ: "ḡ",
	ğ: "ğ",
	ǧ: "ǧ",
	ĝ: "ĝ",
	ġ: "ġ",
	ģ: "ģ",
	ḧ: "ḧ",
	ȟ: "ȟ",
	ĥ: "ĥ",
	ḣ: "ḣ",
	ḩ: "ḩ",
	í: "í",
	ì: "ì",
	ï: "ï",
	ḯ: "ḯ",
	ĩ: "ĩ",
	ī: "ī",
	ĭ: "ĭ",
	ǐ: "ǐ",
	î: "î",
	ǰ: "ǰ",
	ĵ: "ĵ",
	ḱ: "ḱ",
	ǩ: "ǩ",
	ķ: "ķ",
	ĺ: "ĺ",
	ľ: "ľ",
	ļ: "ļ",
	ḿ: "ḿ",
	ṁ: "ṁ",
	ń: "ń",
	ǹ: "ǹ",
	ñ: "ñ",
	ň: "ň",
	ṅ: "ṅ",
	ņ: "ņ",
	ó: "ó",
	ò: "ò",
	ö: "ö",
	ȫ: "ȫ",
	õ: "õ",
	ṍ: "ṍ",
	ṏ: "ṏ",
	ȭ: "ȭ",
	ō: "ō",
	ṓ: "ṓ",
	ṑ: "ṑ",
	ŏ: "ŏ",
	ǒ: "ǒ",
	ô: "ô",
	ố: "ố",
	ồ: "ồ",
	ỗ: "ỗ",
	ȯ: "ȯ",
	ȱ: "ȱ",
	ő: "ő",
	ṕ: "ṕ",
	ṗ: "ṗ",
	ŕ: "ŕ",
	ř: "ř",
	ṙ: "ṙ",
	ŗ: "ŗ",
	ś: "ś",
	ṥ: "ṥ",
	š: "š",
	ṧ: "ṧ",
	ŝ: "ŝ",
	ṡ: "ṡ",
	ş: "ş",
	ẗ: "ẗ",
	ť: "ť",
	ṫ: "ṫ",
	ţ: "ţ",
	ú: "ú",
	ù: "ù",
	ü: "ü",
	ǘ: "ǘ",
	ǜ: "ǜ",
	ǖ: "ǖ",
	ǚ: "ǚ",
	ũ: "ũ",
	ṹ: "ṹ",
	ū: "ū",
	ṻ: "ṻ",
	ŭ: "ŭ",
	ǔ: "ǔ",
	û: "û",
	ů: "ů",
	ű: "ű",
	ṽ: "ṽ",
	ẃ: "ẃ",
	ẁ: "ẁ",
	ẅ: "ẅ",
	ŵ: "ŵ",
	ẇ: "ẇ",
	ẘ: "ẘ",
	ẍ: "ẍ",
	ẋ: "ẋ",
	ý: "ý",
	ỳ: "ỳ",
	ÿ: "ÿ",
	ỹ: "ỹ",
	ȳ: "ȳ",
	ŷ: "ŷ",
	ẏ: "ẏ",
	ẙ: "ẙ",
	ź: "ź",
	ž: "ž",
	ẑ: "ẑ",
	ż: "ż",
	Á: "Á",
	À: "À",
	Ä: "Ä",
	Ǟ: "Ǟ",
	Ã: "Ã",
	Ā: "Ā",
	Ă: "Ă",
	Ắ: "Ắ",
	Ằ: "Ằ",
	Ẵ: "Ẵ",
	Ǎ: "Ǎ",
	Â: "Â",
	Ấ: "Ấ",
	Ầ: "Ầ",
	Ẫ: "Ẫ",
	Ȧ: "Ȧ",
	Ǡ: "Ǡ",
	Å: "Å",
	Ǻ: "Ǻ",
	Ḃ: "Ḃ",
	Ć: "Ć",
	Ḉ: "Ḉ",
	Č: "Č",
	Ĉ: "Ĉ",
	Ċ: "Ċ",
	Ç: "Ç",
	Ď: "Ď",
	Ḋ: "Ḋ",
	Ḑ: "Ḑ",
	É: "É",
	È: "È",
	Ë: "Ë",
	Ẽ: "Ẽ",
	Ē: "Ē",
	Ḗ: "Ḗ",
	Ḕ: "Ḕ",
	Ĕ: "Ĕ",
	Ḝ: "Ḝ",
	Ě: "Ě",
	Ê: "Ê",
	Ế: "Ế",
	Ề: "Ề",
	Ễ: "Ễ",
	Ė: "Ė",
	Ȩ: "Ȩ",
	Ḟ: "Ḟ",
	Ǵ: "Ǵ",
	Ḡ: "Ḡ",
	Ğ: "Ğ",
	Ǧ: "Ǧ",
	Ĝ: "Ĝ",
	Ġ: "Ġ",
	Ģ: "Ģ",
	Ḧ: "Ḧ",
	Ȟ: "Ȟ",
	Ĥ: "Ĥ",
	Ḣ: "Ḣ",
	Ḩ: "Ḩ",
	Í: "Í",
	Ì: "Ì",
	Ï: "Ï",
	Ḯ: "Ḯ",
	Ĩ: "Ĩ",
	Ī: "Ī",
	Ĭ: "Ĭ",
	Ǐ: "Ǐ",
	Î: "Î",
	İ: "İ",
	Ĵ: "Ĵ",
	Ḱ: "Ḱ",
	Ǩ: "Ǩ",
	Ķ: "Ķ",
	Ĺ: "Ĺ",
	Ľ: "Ľ",
	Ļ: "Ļ",
	Ḿ: "Ḿ",
	Ṁ: "Ṁ",
	Ń: "Ń",
	Ǹ: "Ǹ",
	Ñ: "Ñ",
	Ň: "Ň",
	Ṅ: "Ṅ",
	Ņ: "Ņ",
	Ó: "Ó",
	Ò: "Ò",
	Ö: "Ö",
	Ȫ: "Ȫ",
	Õ: "Õ",
	Ṍ: "Ṍ",
	Ṏ: "Ṏ",
	Ȭ: "Ȭ",
	Ō: "Ō",
	Ṓ: "Ṓ",
	Ṑ: "Ṑ",
	Ŏ: "Ŏ",
	Ǒ: "Ǒ",
	Ô: "Ô",
	Ố: "Ố",
	Ồ: "Ồ",
	Ỗ: "Ỗ",
	Ȯ: "Ȯ",
	Ȱ: "Ȱ",
	Ő: "Ő",
	Ṕ: "Ṕ",
	Ṗ: "Ṗ",
	Ŕ: "Ŕ",
	Ř: "Ř",
	Ṙ: "Ṙ",
	Ŗ: "Ŗ",
	Ś: "Ś",
	Ṥ: "Ṥ",
	Š: "Š",
	Ṧ: "Ṧ",
	Ŝ: "Ŝ",
	Ṡ: "Ṡ",
	Ş: "Ş",
	Ť: "Ť",
	Ṫ: "Ṫ",
	Ţ: "Ţ",
	Ú: "Ú",
	Ù: "Ù",
	Ü: "Ü",
	Ǘ: "Ǘ",
	Ǜ: "Ǜ",
	Ǖ: "Ǖ",
	Ǚ: "Ǚ",
	Ũ: "Ũ",
	Ṹ: "Ṹ",
	Ū: "Ū",
	Ṻ: "Ṻ",
	Ŭ: "Ŭ",
	Ǔ: "Ǔ",
	Û: "Û",
	Ů: "Ů",
	Ű: "Ű",
	Ṽ: "Ṽ",
	Ẃ: "Ẃ",
	Ẁ: "Ẁ",
	Ẅ: "Ẅ",
	Ŵ: "Ŵ",
	Ẇ: "Ẇ",
	Ẍ: "Ẍ",
	Ẋ: "Ẋ",
	Ý: "Ý",
	Ỳ: "Ỳ",
	Ÿ: "Ÿ",
	Ỹ: "Ỹ",
	Ȳ: "Ȳ",
	Ŷ: "Ŷ",
	Ẏ: "Ẏ",
	Ź: "Ź",
	Ž: "Ž",
	Ẑ: "Ẑ",
	Ż: "Ż",
	ά: "ά",
	ὰ: "ὰ",
	ᾱ: "ᾱ",
	ᾰ: "ᾰ",
	έ: "έ",
	ὲ: "ὲ",
	ή: "ή",
	ὴ: "ὴ",
	ί: "ί",
	ὶ: "ὶ",
	ϊ: "ϊ",
	ΐ: "ΐ",
	ῒ: "ῒ",
	ῑ: "ῑ",
	ῐ: "ῐ",
	ό: "ό",
	ὸ: "ὸ",
	ύ: "ύ",
	ὺ: "ὺ",
	ϋ: "ϋ",
	ΰ: "ΰ",
	ῢ: "ῢ",
	ῡ: "ῡ",
	ῠ: "ῠ",
	ώ: "ώ",
	ὼ: "ὼ",
	Ύ: "Ύ",
	Ὺ: "Ὺ",
	Ϋ: "Ϋ",
	Ῡ: "Ῡ",
	Ῠ: "Ῠ",
	Ώ: "Ώ",
	Ὼ: "Ὼ"
}, Hc = class e {
	constructor(e, t) {
		this.mode = void 0, this.gullet = void 0, this.settings = void 0, this.leftrightDepth = void 0, this.nextToken = void 0, this.mode = "math", this.gullet = new Lc(e, t, this.mode), this.settings = t, this.leftrightDepth = 0, this.nextToken = null;
	}
	expect(e, t) {
		if (t === void 0 && (t = !0), this.fetch().text !== e) throw new N("Expected '" + e + "', got '" + this.fetch().text + "'", this.fetch());
		t && this.consume();
	}
	consume() {
		this.nextToken = null;
	}
	fetch() {
		return this.nextToken ??= this.gullet.expandNextToken(), this.nextToken;
	}
	switchMode(e) {
		this.mode = e, this.gullet.switchMode(e);
	}
	parse() {
		this.settings.globalGroup || this.gullet.beginGroup(), this.settings.colorIsTextColor && this.gullet.macros.set("\\color", "\\textcolor");
		try {
			var e = this.parseExpression(!1);
			return this.expect("EOF"), this.settings.globalGroup || this.gullet.endGroup(), e;
		} finally {
			this.gullet.endGroups();
		}
	}
	subparse(e) {
		var t = this.nextToken;
		this.consume(), this.gullet.pushToken(new Ds("}")), this.gullet.pushTokens(e);
		var n = this.parseExpression(!1);
		return this.expect("}"), this.nextToken = t, n;
	}
	parseExpression(t, n) {
		for (var r = [];;) {
			this.mode === "math" && this.consumeSpaces();
			var i = this.fetch();
			if (e.endOfExpression.has(i.text) || n && i.text === n || t && _c[i.text] && _c[i.text].infix) break;
			var a = this.parseAtom(n);
			if (!a) break;
			a.type !== "internal" && r.push(a);
		}
		return this.mode === "text" && this.formLigatures(r), this.handleInfixNodes(r);
	}
	handleInfixNodes(e) {
		for (var t = -1, n, r = 0; r < e.length; r++) {
			var i = e[r];
			if (i.type === "infix") {
				if (t !== -1) throw new N("only one infix operator per group", i.token);
				t = r, n = i.replaceWith;
			}
		}
		if (t !== -1 && n) {
			var a, o, s = e.slice(0, t), c = e.slice(t + 1);
			return a = s.length === 1 && s[0].type === "ordgroup" ? s[0] : {
				type: "ordgroup",
				mode: this.mode,
				body: s
			}, o = c.length === 1 && c[0].type === "ordgroup" ? c[0] : {
				type: "ordgroup",
				mode: this.mode,
				body: c
			}, [n === "\\\\abovefrac" ? this.callFunction(n, [
				a,
				e[t],
				o
			], []) : this.callFunction(n, [a, o], [])];
		} else return e;
	}
	handleSupSubscript(e) {
		var t = this.fetch(), n = t.text;
		this.consume(), this.consumeSpaces();
		var r;
		do
			r = this.parseGroup(e);
		while (r?.type === "internal");
		if (!r) throw new N("Expected group after '" + n + "'", t);
		return r;
	}
	formatUnsupportedCmd(e) {
		for (var t = [], n = 0; n < e.length; n++) t.push({
			type: "textord",
			mode: "text",
			text: e[n]
		});
		var r = {
			type: "text",
			mode: this.mode,
			body: t
		};
		return {
			type: "color",
			mode: this.mode,
			color: this.settings.errorColor,
			body: [r]
		};
	}
	parseAtom(e) {
		var t = this.parseGroup("atom", e);
		if (t?.type === "internal" || this.mode === "text") return t;
		for (var n, r;;) {
			this.consumeSpaces();
			var i = this.fetch();
			if (i.text === "\\limits" || i.text === "\\nolimits") {
				if (t && t.type === "op") t.limits = i.text === "\\limits", t.alwaysHandleSupSub = !0;
				else if (t && t.type === "operatorname") t.alwaysHandleSupSub && (t.limits = i.text === "\\limits");
				else throw new N("Limit controls must follow a math operator", i);
				this.consume();
			} else if (i.text === "^") {
				if (n) throw new N("Double superscript", i);
				n = this.handleSupSubscript("superscript");
			} else if (i.text === "_") {
				if (r) throw new N("Double subscript", i);
				r = this.handleSupSubscript("subscript");
			} else if (i.text === "'") {
				if (n) throw new N("Double superscript", i);
				var a = {
					type: "textord",
					mode: this.mode,
					text: "\\prime"
				}, o = [a];
				for (this.consume(); this.fetch().text === "'";) o.push(a), this.consume();
				this.fetch().text === "^" && o.push(this.handleSupSubscript("superscript")), n = {
					type: "ordgroup",
					mode: this.mode,
					body: o
				};
			} else if (zc[i.text]) {
				var s = Rc.test(i.text), c = [];
				for (c.push(new Ds(zc[i.text])), this.consume();;) {
					var l = this.fetch().text;
					if (!zc[l] || Rc.test(l) !== s) break;
					c.unshift(new Ds(zc[l])), this.consume();
				}
				var u = this.subparse(c);
				s ? r = {
					type: "ordgroup",
					mode: "math",
					body: u
				} : n = {
					type: "ordgroup",
					mode: "math",
					body: u
				};
			} else break;
		}
		return n || r ? {
			type: "supsub",
			mode: this.mode,
			base: t,
			sup: n,
			sub: r
		} : t;
	}
	parseFunction(e, t) {
		var n = this.fetch(), r = n.text, i = _c[r];
		if (!i) return null;
		if (this.consume(), t && t !== "atom" && !i.allowedInArgument) throw new N("Got function '" + r + "' with no arguments" + (t ? " as " + t : ""), n);
		if (this.mode === "text" && !i.allowedInText) throw new N("Can't use function '" + r + "' in text mode", n);
		if (this.mode === "math" && i.allowedInMath === !1) throw new N("Can't use function '" + r + "' in math mode", n);
		var { args: a, optArgs: o } = this.parseArguments(r, i);
		return this.callFunction(r, a, o, n, e);
	}
	callFunction(e, t, n, r, i) {
		var a = {
			funcName: e,
			parser: this,
			token: r,
			breakOnTokenText: i
		}, o = _c[e];
		if (o && o.handler) return o.handler(a, t, n);
		throw new N("No function handler for " + e);
	}
	parseArguments(e, t) {
		var n = t.numArgs + t.numOptionalArgs;
		if (n === 0) return {
			args: [],
			optArgs: []
		};
		for (var r = [], i = [], a = 0; a < n; a++) {
			var o = t.argTypes && t.argTypes[a], s = a < t.numOptionalArgs;
			("primitive" in t && t.primitive && o == null || t.type === "sqrt" && a === 1 && i[0] == null) && (o = "primitive");
			var c = this.parseGroupOfType("argument to '" + e + "'", o, s);
			if (s) i.push(c);
			else if (c != null) r.push(c);
			else throw new N("Null argument, please report this as a bug");
		}
		return {
			args: r,
			optArgs: i
		};
	}
	parseGroupOfType(e, t, n) {
		switch (t) {
			case "color": return this.parseColorGroup(n);
			case "size": return this.parseSizeGroup(n);
			case "url": return this.parseUrlGroup(n);
			case "math":
			case "text": return this.parseArgumentGroup(n, t);
			case "hbox":
				var r = this.parseArgumentGroup(n, "text");
				return r == null ? null : {
					type: "styling",
					mode: r.mode,
					body: [r],
					style: "text",
					resetFont: !0
				};
			case "raw":
				var i = this.parseStringGroup("raw", n);
				return i == null ? null : {
					type: "raw",
					mode: "text",
					string: i.text
				};
			case "primitive":
				if (n) throw new N("A primitive argument cannot be optional");
				var a = this.parseGroup(e);
				if (a == null) throw new N("Expected group as " + e, this.fetch());
				return a;
			case "original":
			case null:
			case void 0: return this.parseArgumentGroup(n);
			default: throw new N("Unknown group type as " + e, this.fetch());
		}
	}
	consumeSpaces() {
		for (; this.fetch().text === " ";) this.consume();
	}
	parseStringGroup(e, t) {
		var n = this.gullet.scanArgument(t);
		if (n == null) return null;
		for (var r = "", i; (i = this.fetch()).text !== "EOF";) r += i.text, this.consume();
		return this.consume(), n.text = r, n;
	}
	parseRegexGroup(e, t) {
		for (var n = this.fetch(), r = n, i = "", a; (a = this.fetch()).text !== "EOF" && e.test(i + a.text);) r = a, i += r.text, this.consume();
		if (i === "") throw new N("Invalid " + t + ": '" + n.text + "'", n);
		return n.range(r, i);
	}
	parseColorGroup(e) {
		var t = this.parseStringGroup("color", e);
		if (t == null) return null;
		var n = /^(#[a-f0-9]{3,4}|#[a-f0-9]{6}|#[a-f0-9]{8}|[a-f0-9]{6}|[a-z]+)$/i.exec(t.text);
		if (!n) throw new N("Invalid color: '" + t.text + "'", t);
		var r = n[0];
		return /^[0-9a-f]{6}$/i.test(r) && (r = "#" + r), {
			type: "color-token",
			mode: this.mode,
			color: r
		};
	}
	parseSizeGroup(e) {
		var t, n = !1;
		if (this.gullet.consumeSpaces(), t = !e && this.gullet.future().text !== "{" ? this.parseRegexGroup(/^[-+]? *(?:$|\d+|\d+\.\d*|\.\d*) *[a-z]{0,2} *$/, "size") : this.parseStringGroup("size", e), !t) return null;
		!e && t.text.length === 0 && (t.text = "0pt", n = !0);
		var r = /([-+]?) *(\d+(?:\.\d*)?|\.\d+) *([a-z]{2})/.exec(t.text);
		if (!r) throw new N("Invalid size: '" + t.text + "'", t);
		var i = {
			number: +(r[1] + r[2]),
			unit: r[3]
		};
		if (!Vr(i)) throw new N("Invalid unit: '" + i.unit + "'", t);
		return {
			type: "size",
			mode: this.mode,
			value: i,
			isBlank: n
		};
	}
	parseUrlGroup(e) {
		this.gullet.lexer.setCatcode("%", 13), this.gullet.lexer.setCatcode("~", 12);
		var t = this.parseStringGroup("url", e);
		if (this.gullet.lexer.setCatcode("%", 14), this.gullet.lexer.setCatcode("~", 13), t == null) return null;
		var n = t.text.replace(/\\([#$%&~_^{}])/g, "$1");
		return {
			type: "url",
			mode: this.mode,
			url: n
		};
	}
	parseArgumentGroup(e, t) {
		var n = this.gullet.scanArgument(e);
		if (n == null) return null;
		var r = this.mode;
		t && this.switchMode(t), this.gullet.beginGroup();
		var i = this.parseExpression(!1, "EOF");
		this.expect("EOF"), this.gullet.endGroup();
		var a = {
			type: "ordgroup",
			mode: this.mode,
			loc: n.loc,
			body: i
		};
		return t && this.switchMode(r), a;
	}
	parseGroup(e, t) {
		var n = this.fetch(), r = n.text, i;
		if (r === "{" || r === "\\begingroup") {
			this.consume();
			var a = r === "{" ? "}" : "\\endgroup";
			this.gullet.beginGroup();
			var o = this.parseExpression(!1, a), s = this.fetch();
			this.expect(a), this.gullet.endGroup(), i = {
				type: "ordgroup",
				mode: this.mode,
				loc: Es.range(n, s),
				body: o,
				semisimple: r === "\\begingroup" || void 0
			};
		} else if (i = this.parseFunction(t, e) || this.parseSymbol(), i == null && r[0] === "\\" && !Ic.hasOwnProperty(r)) {
			if (this.settings.throwOnError) throw new N("Undefined control sequence: " + r, n);
			i = this.formatUnsupportedCmd(r), this.consume();
		}
		return i;
	}
	formLigatures(e) {
		for (var t = e.length - 1, n = 0; n < t; ++n) {
			var r = e[n];
			if (r.type === "textord") {
				var i = r.text, a = e[n + 1];
				if (!(!a || a.type !== "textord")) {
					if (i === "-" && a.text === "-") {
						var o = e[n + 2];
						n + 1 < t && o && o.type === "textord" && o.text === "-" ? (e.splice(n, 3, {
							type: "textord",
							mode: "text",
							loc: Es.range(r, o),
							text: "---"
						}), t -= 2) : (e.splice(n, 2, {
							type: "textord",
							mode: "text",
							loc: Es.range(r, a),
							text: "--"
						}), --t);
					}
					(i === "'" || i === "`") && a.text === i && (e.splice(n, 2, {
						type: "textord",
						mode: "text",
						loc: Es.range(r, a),
						text: i + i
					}), --t);
				}
			}
		}
	}
	parseSymbol() {
		var e = this.fetch(), t = e.text;
		if (/^\\verb[^a-zA-Z]/.test(t)) {
			this.consume();
			var n = t.slice(5), r = n.charAt(0) === "*";
			if (r && (n = n.slice(1)), n.length < 2 || n.charAt(0) !== n.slice(-1)) throw new N("\\verb assertion failed --\n                    please report what input caused this bug");
			return n = n.slice(1, -1), {
				type: "verb",
				mode: "text",
				body: n,
				star: r
			};
		}
		Vc.hasOwnProperty(t[0]) && !pi[this.mode][t[0]] && (this.settings.strict && this.mode === "math" && this.settings.reportNonstrict("unicodeTextInMathMode", "Accented Unicode text character \"" + t[0] + "\" used in math mode", e), t = Vc[t[0]] + t.slice(1));
		var i = wc.exec(t);
		i && (t = t.substring(0, i.index), t === "i" ? t = "ı" : t === "j" && (t = "ȷ"));
		var a;
		if (pi[this.mode][t]) {
			this.settings.strict && this.mode === "math" && Fi.includes(t) && this.settings.reportNonstrict("unicodeTextInMathMode", "Latin-1/Unicode text character \"" + t[0] + "\" used in math mode", e);
			var o = pi[this.mode][t].group, s = Es.range(e);
			a = vo(o) ? {
				type: "atom",
				mode: this.mode,
				family: o,
				loc: s,
				text: t
			} : {
				type: o,
				mode: this.mode,
				loc: s,
				text: t
			};
		} else if (t.charCodeAt(0) >= 128) this.settings.strict && (Cr(t.charCodeAt(0)) ? this.mode === "math" && this.settings.reportNonstrict("unicodeTextInMathMode", "Unicode text character \"" + t[0] + "\" used in math mode", e) : this.settings.reportNonstrict("unknownSymbol", "Unrecognized Unicode character \"" + t[0] + "\"" + (" (" + t.charCodeAt(0) + ")"), e)), a = {
			type: "textord",
			mode: "text",
			loc: Es.range(e),
			text: t
		};
		else return null;
		if (this.consume(), i) for (var c = 0; c < i[0].length; c++) {
			var l = i[0][c];
			if (!Bc[l]) throw new N("Unknown accent ' " + l + "'", e);
			var u = Bc[l][this.mode] || Bc[l].text;
			if (!u) throw new N("Accent " + l + " unsupported in " + this.mode + " mode", e);
			a = {
				type: "accent",
				mode: this.mode,
				loc: Es.range(e),
				label: u,
				isStretchy: !1,
				isShifty: !0,
				base: a
			};
		}
		return a;
	}
};
Hc.endOfExpression = new Set([
	"}",
	"\\endgroup",
	"\\end",
	"\\right",
	"&"
]);
var Uc = function(e, t) {
	if (!(typeof e == "string" || e instanceof String)) throw TypeError("KaTeX can only parse string typed expression");
	var n = new Hc(e, t);
	delete n.gullet.macros.current["\\df@tag"];
	var r = n.parse();
	if (delete n.gullet.macros.current["\\current@color"], delete n.gullet.macros.current["\\color"], n.gullet.macros.get("\\df@tag")) {
		if (!t.displayMode) throw new N("\\tag works only in display equations");
		r = [{
			type: "tag",
			mode: "text",
			body: r,
			tag: n.subparse([new Ds("\\df@tag")])
		}];
	}
	return r;
}, Wc = function(e, t, n) {
	t.textContent = "";
	var r = Jc(e, n).toNode();
	t.appendChild(r);
};
typeof document < "u" && document.compatMode !== "CSS1Compat" && (typeof console < "u" && console.warn("Warning: KaTeX doesn't work in quirks mode. Make sure your website has a suitable doctype."), Wc = function() {
	throw new N("KaTeX doesn't work in quirks mode.");
});
var Gc = function(e, t) {
	return Jc(e, t).toMarkup();
}, Kc = function(e, t) {
	return Uc(e, new rr(t));
}, qc = function(e, t, n) {
	if (n.throwOnError || !(e instanceof N)) throw e;
	var r = K(["katex-error"], [new $r(t)]);
	return r.setAttribute("title", e.toString()), r.setAttribute("style", "color:" + n.errorColor), r;
}, Jc = function(e, t) {
	var n = new rr(t);
	try {
		return so(Uc(e, n), e, n);
	} catch (t) {
		return qc(t, e, n);
	}
}, Yc = {
	version: "0.16.47",
	render: Wc,
	renderToString: Gc,
	ParseError: N,
	SETTINGS_SCHEMA: $n,
	__parse: Kc,
	__renderToDomTree: Jc,
	__renderToHTMLTree: function(e, t) {
		var n = new rr(t);
		try {
			return co(Uc(e, n), e, n);
		} catch (t) {
			return qc(t, e, n);
		}
	},
	__setFontMetrics: li,
	__defineSymbol: I,
	__defineFunction: q,
	__defineMacro: Q,
	__domTree: {
		Span: Yr,
		Anchor: Xr,
		SymbolNode: $r,
		SvgNode: ei,
		PathNode: ti,
		LineNode: ni
	}
};
//#endregion
//#region frontend/node_modules/unist-util-visit-parents/lib/color.js
function Xc(e) {
	return e;
}
//#endregion
//#region frontend/node_modules/unist-util-visit-parents/lib/index.js
var Zc = [], Qc = "skip";
function $c(e, t, n, r) {
	let i;
	typeof t == "function" && typeof n != "function" ? (r = n, n = t) : i = t;
	let a = rt(i), o = r ? -1 : 1;
	s(e, void 0, [])();
	function s(e, i, c) {
		let l = e && typeof e == "object" ? e : {};
		if (typeof l.type == "string") {
			let t = typeof l.tagName == "string" ? l.tagName : typeof l.name == "string" ? l.name : void 0;
			Object.defineProperty(u, "name", { value: "node (" + Xc(e.type + (t ? "<" + t + ">" : "")) + ")" });
		}
		return u;
		function u() {
			let l = Zc, u, d, f;
			if ((!t || a(e, i, c[c.length - 1] || void 0)) && (l = el(n(e, c)), l[0] === !1)) return l;
			if ("children" in e && e.children) {
				let t = e;
				if (t.children && l[0] !== "skip") for (d = (r ? t.children.length : -1) + o, f = c.concat(t); d > -1 && d < t.children.length;) {
					let e = t.children[d];
					if (u = s(e, d, f)(), u[0] === !1) return u;
					d = typeof u[1] == "number" ? u[1] : d + o;
				}
			}
			return l;
		}
	}
}
function el(e) {
	return Array.isArray(e) ? e : typeof e == "number" ? [!0, e] : e == null ? Zc : [e];
}
//#endregion
//#region frontend/node_modules/rehype-katex/lib/index.js
var tl = {}, nl = [];
function rl(e) {
	let t = e || tl;
	return function(e, n) {
		$c(e, "element", function(e, r) {
			let i = Array.isArray(e.properties.className) ? e.properties.className : nl, a = i.includes("language-math"), o = i.includes("math-display"), s = i.includes("math-inline"), c = o;
			if (!a && !o && !s) return;
			let l = r[r.length - 1], u = e;
			/* c8 ignore next -- verbose to test. */
			if (e.tagName === "code" && a && l && l.type === "element" && l.tagName === "pre" && (u = l, l = r[r.length - 2], c = !0), !l) return;
			let d = Tt(u, { whitespace: "pre" }), f;
			try {
				f = Yc.renderToString(d, {
					...t,
					displayMode: c,
					throwOnError: !0
				});
			} catch (i) {
				let a = i, o = a.name.toLowerCase();
				n.message("Could not render math with KaTeX", {
					ancestors: [...r, e],
					cause: a,
					place: e.position,
					ruleId: o,
					source: "rehype-katex"
				});
				try {
					f = Yc.renderToString(d, {
						...t,
						displayMode: c,
						strict: "ignore",
						throwOnError: !1
					});
				} catch {
					f = [{
						type: "element",
						tagName: "span",
						properties: {
							className: ["katex-error"],
							style: "color:" + (t.errorColor || "#cc0000"),
							title: String(i)
						},
						children: [{
							type: "text",
							value: d
						}]
					}];
				}
			}
			typeof f == "string" && (f = Hn(f, { fragment: !0 }).children);
			let p = l.children.indexOf(u);
			return l.children.splice(p, 1, ...f), Qc;
		});
	};
}
//#endregion
//#region frontend/node_modules/estree-util-is-identifier-name/lib/index.js
var il = /^[$_\p{ID_Start}][$_\u{200C}\u{200D}\p{ID_Continue}]*$/u, al = /^[$_\p{ID_Start}][-$_\u{200C}\u{200D}\p{ID_Continue}]*$/u, ol = {};
function sl(e, t) {
	return ((t || ol).jsx ? al : il).test(e);
}
//#endregion
//#region frontend/node_modules/hast-util-whitespace/lib/index.js
var cl = /[ \t\n\f\r]/g;
function ll(e) {
	return typeof e == "object" ? e.type === "text" ? ul(e.value) : !1 : ul(e);
}
function ul(e) {
	return e.replace(cl, "") === "";
}
//#endregion
//#region frontend/node_modules/inline-style-parser/cjs/index.js
var dl = /* @__PURE__ */ o(((e, t) => {
	var n = /\/\*[^*]*\*+([^/*][^*]*\*+)*\//g, r = /\n/g, i = /^\s*/, a = /^(\*?[-#/*\\\w]+(\[[0-9a-z_-]+\])?)\s*/, o = /^:\s*/, s = /^((?:'(?:\\'|.)*?'|"(?:\\"|.)*?"|\([^)]*?\)|[^};])+)/, c = /^[;\s]*/, l = /^\s+|\s+$/g, u = "\n", d = "/", f = "*", p = "", m = "comment", h = "declaration";
	function g(e, t) {
		if (typeof e != "string") throw TypeError("First argument must be a string");
		if (!e) return [];
		t ||= {};
		var l = 1, g = 1;
		function v(e) {
			var t = e.match(r);
			t && (l += t.length);
			var n = e.lastIndexOf(u);
			g = ~n ? e.length - n : g + e.length;
		}
		function y() {
			var e = {
				line: l,
				column: g
			};
			return function(t) {
				return t.position = new b(e), C(), t;
			};
		}
		function b(e) {
			this.start = e, this.end = {
				line: l,
				column: g
			}, this.source = t.source;
		}
		b.prototype.content = e;
		function x(n) {
			var r = /* @__PURE__ */ Error(t.source + ":" + l + ":" + g + ": " + n);
			if (r.reason = n, r.filename = t.source, r.line = l, r.column = g, r.source = e, !t.silent) throw r;
		}
		function S(t) {
			var n = t.exec(e);
			if (n) {
				var r = n[0];
				return v(r), e = e.slice(r.length), n;
			}
		}
		function C() {
			S(i);
		}
		function w(e) {
			var t;
			for (e ||= []; t = T();) t !== !1 && e.push(t);
			return e;
		}
		function T() {
			var t = y();
			if (!(d != e.charAt(0) || f != e.charAt(1))) {
				for (var n = 2; p != e.charAt(n) && (f != e.charAt(n) || d != e.charAt(n + 1));) ++n;
				if (n += 2, p === e.charAt(n - 1)) return x("End of comment missing");
				var r = e.slice(2, n - 2);
				return g += 2, v(r), e = e.slice(n), g += 2, t({
					type: m,
					comment: r
				});
			}
		}
		function E() {
			var e = y(), t = S(a);
			if (t) {
				if (T(), !S(o)) return x("property missing ':'");
				var r = S(s), i = e({
					type: h,
					property: _(t[0].replace(n, p)),
					value: r ? _(r[0].replace(n, p)) : p
				});
				return S(c), i;
			}
		}
		function ee() {
			var e = [];
			w(e);
			for (var t; t = E();) t !== !1 && (e.push(t), w(e));
			return e;
		}
		return C(), ee();
	}
	function _(e) {
		return e ? e.replace(l, p) : p;
	}
	t.exports = g;
})), fl = /* @__PURE__ */ o(((e) => {
	var t = e && e.__importDefault || function(e) {
		return e && e.__esModule ? e : { default: e };
	};
	Object.defineProperty(e, "__esModule", { value: !0 }), e.default = r;
	var n = t(dl());
	function r(e, t) {
		let r = null;
		if (!e || typeof e != "string") return r;
		let i = (0, n.default)(e), a = typeof t == "function";
		return i.forEach((e) => {
			if (e.type !== "declaration") return;
			let { property: n, value: i } = e;
			a ? t(n, i, e) : i && (r ||= {}, r[n] = i);
		}), r;
	}
})), pl = /* @__PURE__ */ o(((e) => {
	Object.defineProperty(e, "__esModule", { value: !0 }), e.camelCase = void 0;
	var t = /^--[a-zA-Z0-9_-]+$/, n = /-([a-z])/g, r = /^[^-]+$/, i = /^-(webkit|moz|ms|o|khtml)-/, a = /^-(ms)-/, o = function(e) {
		return !e || r.test(e) || t.test(e);
	}, s = function(e, t) {
		return t.toUpperCase();
	}, c = function(e, t) {
		return `${t}-`;
	};
	e.camelCase = function(e, t) {
		return t === void 0 && (t = {}), o(e) ? e : (e = e.toLowerCase(), e = t.reactCompat ? e.replace(a, c) : e.replace(i, c), e.replace(n, s));
	};
})), ml = /* @__PURE__ */ o(((e, t) => {
	var n = (e && e.__importDefault || function(e) {
		return e && e.__esModule ? e : { default: e };
	})(fl()), r = pl();
	function i(e, t) {
		var i = {};
		return !e || typeof e != "string" || (0, n.default)(e, function(e, n) {
			e && n && (i[(0, r.camelCase)(e, t)] = n);
		}), i;
	}
	i.default = i, t.exports = i;
})), hl = _l("end"), gl = _l("start");
function _l(e) {
	return t;
	function t(t) {
		let n = t && t.position && t.position[e] || {};
		if (typeof n.line == "number" && n.line > 0 && typeof n.column == "number" && n.column > 0) return {
			line: n.line,
			column: n.column,
			offset: typeof n.offset == "number" && n.offset > -1 ? n.offset : void 0
		};
	}
}
function vl(e) {
	let t = gl(e), n = hl(e);
	if (t && n) return {
		start: t,
		end: n
	};
}
//#endregion
//#region frontend/node_modules/unist-util-stringify-position/lib/index.js
function yl(e) {
	return !e || typeof e != "object" ? "" : "position" in e || "type" in e ? xl(e.position) : "start" in e || "end" in e ? xl(e) : "line" in e || "column" in e ? bl(e) : "";
}
function bl(e) {
	return Sl(e && e.line) + ":" + Sl(e && e.column);
}
function xl(e) {
	return bl(e && e.start) + "-" + bl(e && e.end);
}
function Sl(e) {
	return e && typeof e == "number" ? e : 1;
}
//#endregion
//#region frontend/node_modules/vfile-message/lib/index.js
var Cl = class extends Error {
	constructor(e, t, n) {
		super(), typeof t == "string" && (n = t, t = void 0);
		let r = "", i = {}, a = !1;
		if (t && (i = "line" in t && "column" in t || "start" in t && "end" in t ? { place: t } : "type" in t ? {
			ancestors: [t],
			place: t.position
		} : { ...t }), typeof e == "string" ? r = e : !i.cause && e && (a = !0, r = e.message, i.cause = e), !i.ruleId && !i.source && typeof n == "string") {
			let e = n.indexOf(":");
			e === -1 ? i.ruleId = n : (i.source = n.slice(0, e), i.ruleId = n.slice(e + 1));
		}
		if (!i.place && i.ancestors && i.ancestors) {
			let e = i.ancestors[i.ancestors.length - 1];
			e && (i.place = e.position);
		}
		let o = i.place && "start" in i.place ? i.place.start : i.place;
		this.ancestors = i.ancestors || void 0, this.cause = i.cause || void 0, this.column = o ? o.column : void 0, this.fatal = void 0, this.file = "", this.message = r, this.line = o ? o.line : void 0, this.name = yl(i.place) || "1:1", this.place = i.place || void 0, this.reason = this.message, this.ruleId = i.ruleId || void 0, this.source = i.source || void 0, this.stack = a && i.cause && typeof i.cause.stack == "string" ? i.cause.stack : "", this.actual = void 0, this.expected = void 0, this.note = void 0, this.url = void 0;
	}
};
Cl.prototype.file = "", Cl.prototype.name = "", Cl.prototype.reason = "", Cl.prototype.message = "", Cl.prototype.stack = "", Cl.prototype.column = void 0, Cl.prototype.line = void 0, Cl.prototype.ancestors = void 0, Cl.prototype.cause = void 0, Cl.prototype.fatal = void 0, Cl.prototype.place = void 0, Cl.prototype.ruleId = void 0, Cl.prototype.source = void 0;
//#endregion
//#region frontend/node_modules/hast-util-to-jsx-runtime/lib/index.js
var wl = /* @__PURE__ */ l(ml(), 1), Tl = {}.hasOwnProperty, El = /* @__PURE__ */ new Map(), Dl = /[A-Z]/g, Ol = new Set([
	"table",
	"tbody",
	"thead",
	"tfoot",
	"tr"
]), kl = new Set(["td", "th"]), Al = "https://github.com/syntax-tree/hast-util-to-jsx-runtime";
function jl(e, t) {
	if (!t || t.Fragment === void 0) throw TypeError("Expected `Fragment` in options");
	let n = t.filePath || void 0, r;
	if (t.development) {
		if (typeof t.jsxDEV != "function") throw TypeError("Expected `jsxDEV` in options when `development: true`");
		r = Hl(n, t.jsxDEV);
	} else {
		if (typeof t.jsx != "function") throw TypeError("Expected `jsx` in production options");
		if (typeof t.jsxs != "function") throw TypeError("Expected `jsxs` in production options");
		r = Vl(n, t.jsx, t.jsxs);
	}
	let i = {
		Fragment: t.Fragment,
		ancestors: [],
		components: t.components || {},
		create: r,
		elementAttributeNameCase: t.elementAttributeNameCase || "react",
		evaluater: t.createEvaluater ? t.createEvaluater() : void 0,
		filePath: n,
		ignoreInvalidStyle: t.ignoreInvalidStyle || !1,
		passKeys: t.passKeys !== !1,
		passNode: t.passNode || !1,
		schema: t.space === "svg" ? mn : pn,
		stylePropertyNameCase: t.stylePropertyNameCase || "dom",
		tableCellAlignToStyle: t.tableCellAlignToStyle !== !1
	}, a = Ml(i, e, void 0);
	return a && typeof a != "string" ? a : i.create(e, i.Fragment, { children: a || void 0 }, void 0);
}
function Ml(e, t, n) {
	if (t.type === "element") return Nl(e, t, n);
	if (t.type === "mdxFlowExpression" || t.type === "mdxTextExpression") return Pl(e, t);
	if (t.type === "mdxJsxFlowElement" || t.type === "mdxJsxTextElement") return Il(e, t, n);
	if (t.type === "mdxjsEsm") return Fl(e, t);
	if (t.type === "root") return Ll(e, t, n);
	if (t.type === "text") return Rl(e, t);
}
function Nl(e, t, n) {
	let r = e.schema, i = r;
	t.tagName.toLowerCase() === "svg" && r.space === "html" && (i = mn, e.schema = i), e.ancestors.push(t);
	let a = Jl(e, t.tagName, !1), o = Ul(e, t), s = Gl(e, t);
	return Ol.has(t.tagName) && (s = s.filter(function(e) {
		return typeof e == "string" ? !ll(e) : !0;
	})), zl(e, o, a, t), Bl(o, s), e.ancestors.pop(), e.schema = r, e.create(t, a, o, n);
}
function Pl(e, t) {
	if (t.data && t.data.estree && e.evaluater) {
		let n = t.data.estree.body[0];
		return n.type, e.evaluater.evaluateExpression(n.expression);
	}
	Yl(e, t.position);
}
function Fl(e, t) {
	if (t.data && t.data.estree && e.evaluater) return e.evaluater.evaluateProgram(t.data.estree);
	Yl(e, t.position);
}
function Il(e, t, n) {
	let r = e.schema, i = r;
	t.name === "svg" && r.space === "html" && (i = mn, e.schema = i), e.ancestors.push(t);
	let a = t.name === null ? e.Fragment : Jl(e, t.name, !0), o = Wl(e, t), s = Gl(e, t);
	return zl(e, o, a, t), Bl(o, s), e.ancestors.pop(), e.schema = r, e.create(t, a, o, n);
}
function Ll(e, t, n) {
	let r = {};
	return Bl(r, Gl(e, t)), e.create(t, e.Fragment, r, n);
}
function Rl(e, t) {
	return t.value;
}
function zl(e, t, n, r) {
	typeof n != "string" && n !== e.Fragment && e.passNode && (t.node = r);
}
function Bl(e, t) {
	if (t.length > 0) {
		let n = t.length > 1 ? t : t[0];
		n && (e.children = n);
	}
}
function Vl(e, t, n) {
	return r;
	function r(e, r, i, a) {
		let o = Array.isArray(i.children) ? n : t;
		return a ? o(r, i, a) : o(r, i);
	}
}
function Hl(e, t) {
	return n;
	function n(n, r, i, a) {
		let o = Array.isArray(i.children), s = gl(n);
		return t(r, i, a, o, {
			columnNumber: s ? s.column - 1 : void 0,
			fileName: e,
			lineNumber: s ? s.line : void 0
		}, void 0);
	}
}
function Ul(e, t) {
	let n = {}, r, i;
	for (i in t.properties) if (i !== "children" && Tl.call(t.properties, i)) {
		let a = Kl(e, i, t.properties[i]);
		if (a) {
			let [i, o] = a;
			e.tableCellAlignToStyle && i === "align" && typeof o == "string" && kl.has(t.tagName) ? r = o : n[i] = o;
		}
	}
	if (r) {
		let t = n.style ||= {};
		t[e.stylePropertyNameCase === "css" ? "text-align" : "textAlign"] = r;
	}
	return n;
}
function Wl(e, t) {
	let n = {};
	for (let r of t.attributes) if (r.type === "mdxJsxExpressionAttribute") if (r.data && r.data.estree && e.evaluater) {
		let t = r.data.estree.body[0];
		t.type;
		let i = t.expression;
		i.type;
		let a = i.properties[0];
		a.type, Object.assign(n, e.evaluater.evaluateExpression(a.argument));
	} else Yl(e, t.position);
	else {
		let i = r.name, a;
		if (r.value && typeof r.value == "object") if (r.value.data && r.value.data.estree && e.evaluater) {
			let t = r.value.data.estree.body[0];
			t.type, a = e.evaluater.evaluateExpression(t.expression);
		} else Yl(e, t.position);
		else a = r.value === null ? !0 : r.value;
		n[i] = a;
	}
	return n;
}
function Gl(e, t) {
	let n = [], r = -1, i = e.passKeys ? /* @__PURE__ */ new Map() : El;
	for (; ++r < t.children.length;) {
		let a = t.children[r], o;
		if (e.passKeys) {
			let e = a.type === "element" ? a.tagName : a.type === "mdxJsxFlowElement" || a.type === "mdxJsxTextElement" ? a.name : void 0;
			if (e) {
				let t = i.get(e) || 0;
				o = e + "-" + t, i.set(e, t + 1);
			}
		}
		let s = Ml(e, a, o);
		s !== void 0 && n.push(s);
	}
	return n;
}
function Kl(e, t, n) {
	let r = un(e.schema, t);
	if (!(n == null || typeof n == "number" && Number.isNaN(n))) {
		if (Array.isArray(n) && (n = r.commaSeparated ? gn(n) : bn(n)), r.property === "style") {
			let t = typeof n == "object" ? n : ql(e, String(n));
			return e.stylePropertyNameCase === "css" && (t = Xl(t)), ["style", t];
		}
		return [e.elementAttributeNameCase === "react" && r.space ? on[r.property] || r.property : r.attribute, n];
	}
}
function ql(e, t) {
	try {
		return (0, wl.default)(t, { reactCompat: !0 });
	} catch (t) {
		if (e.ignoreInvalidStyle) return {};
		let n = t, r = new Cl("Cannot parse `style` attribute", {
			ancestors: e.ancestors,
			cause: n,
			ruleId: "style",
			source: "hast-util-to-jsx-runtime"
		});
		throw r.file = e.filePath || void 0, r.url = Al + "#cannot-parse-style-attribute", r;
	}
}
function Jl(e, t, n) {
	let r;
	if (!n) r = {
		type: "Literal",
		value: t
	};
	else if (t.includes(".")) {
		let e = t.split("."), n = -1, i;
		for (; ++n < e.length;) {
			let t = sl(e[n]) ? {
				type: "Identifier",
				name: e[n]
			} : {
				type: "Literal",
				value: e[n]
			};
			i = i ? {
				type: "MemberExpression",
				object: i,
				property: t,
				computed: !!(n && t.type === "Literal"),
				optional: !1
			} : t;
		}
		r = i;
	} else r = sl(t) && !/^[a-z]/.test(t) ? {
		type: "Identifier",
		name: t
	} : {
		type: "Literal",
		value: t
	};
	if (r.type === "Literal") {
		let t = r.value;
		return Tl.call(e.components, t) ? e.components[t] : t;
	}
	if (e.evaluater) return e.evaluater.evaluateExpression(r);
	Yl(e);
}
function Yl(e, t) {
	let n = new Cl("Cannot handle MDX estrees without `createEvaluater`", {
		ancestors: e.ancestors,
		place: t,
		ruleId: "mdx-estree",
		source: "hast-util-to-jsx-runtime"
	});
	throw n.file = e.filePath || void 0, n.url = Al + "#cannot-handle-mdx-estrees-without-createevaluater", n;
}
function Xl(e) {
	let t = {}, n;
	for (n in e) Tl.call(e, n) && (t[Zl(n)] = e[n]);
	return t;
}
function Zl(e) {
	let t = e.replace(Dl, Ql);
	return t.slice(0, 3) === "ms-" && (t = "-" + t), t;
}
function Ql(e) {
	return "-" + e.toLowerCase();
}
//#endregion
//#region frontend/node_modules/html-url-attributes/lib/index.js
var $l = {
	action: ["form"],
	cite: [
		"blockquote",
		"del",
		"ins",
		"q"
	],
	data: ["object"],
	formAction: ["button", "input"],
	href: [
		"a",
		"area",
		"base",
		"link"
	],
	icon: ["menuitem"],
	itemId: null,
	manifest: ["html"],
	ping: ["a", "area"],
	poster: ["video"],
	src: [
		"audio",
		"embed",
		"iframe",
		"img",
		"input",
		"script",
		"source",
		"track",
		"video"
	]
}, eu = {};
function tu(e, t) {
	let n = t || eu;
	return nu(e, typeof n.includeImageAlt == "boolean" ? n.includeImageAlt : !0, typeof n.includeHtml == "boolean" ? n.includeHtml : !0);
}
function nu(e, t, n) {
	if (iu(e)) {
		if ("value" in e) return e.type === "html" && !n ? "" : e.value;
		if (t && "alt" in e && e.alt) return e.alt;
		if ("children" in e) return ru(e.children, t, n);
	}
	return Array.isArray(e) ? ru(e, t, n) : "";
}
function ru(e, t, n) {
	let r = [], i = -1;
	for (; ++i < e.length;) r[i] = nu(e[i], t, n);
	return r.join("");
}
function iu(e) {
	return !!(e && typeof e == "object");
}
//#endregion
//#region frontend/node_modules/decode-named-character-reference/index.dom.js
var au = document.createElement("i");
function ou(e) {
	let t = "&" + e + ";";
	au.innerHTML = t;
	let n = au.textContent;
	return n.charCodeAt(n.length - 1) === 59 && e !== "semi" || n === t ? !1 : n;
}
//#endregion
//#region frontend/node_modules/micromark-util-chunked/index.js
function su(e, t, n, r) {
	let i = e.length, a = 0, o;
	if (t = t < 0 ? -t > i ? 0 : i + t : t > i ? i : t, n = n > 0 ? n : 0, r.length < 1e4) o = Array.from(r), o.unshift(t, n), e.splice(...o);
	else for (n && e.splice(t, n); a < r.length;) o = r.slice(a, a + 1e4), o.unshift(t, 0), e.splice(...o), a += 1e4, t += 1e4;
}
function cu(e, t) {
	return e.length > 0 ? (su(e, e.length, 0, t), e) : t;
}
//#endregion
//#region frontend/node_modules/micromark-util-combine-extensions/index.js
var lu = {}.hasOwnProperty;
function uu(e) {
	let t = {}, n = -1;
	for (; ++n < e.length;) du(t, e[n]);
	return t;
}
function du(e, t) {
	let n;
	for (n in t) {
		let r = (lu.call(e, n) ? e[n] : void 0) || (e[n] = {}), i = t[n], a;
		if (i) for (a in i) {
			lu.call(r, a) || (r[a] = []);
			let e = i[a];
			fu(r[a], Array.isArray(e) ? e : e ? [e] : []);
		}
	}
}
function fu(e, t) {
	let n = -1, r = [];
	for (; ++n < t.length;) (t[n].add === "after" ? e : r).push(t[n]);
	su(e, 0, 0, r);
}
//#endregion
//#region frontend/node_modules/micromark-util-decode-numeric-character-reference/index.js
function pu(e, t) {
	let n = Number.parseInt(e, t);
	return n < 9 || n === 11 || n > 13 && n < 32 || n > 126 && n < 160 || n > 55295 && n < 57344 || n > 64975 && n < 65008 || (n & 65535) == 65535 || (n & 65535) == 65534 || n > 1114111 ? "�" : String.fromCodePoint(n);
}
//#endregion
//#region frontend/node_modules/micromark-util-normalize-identifier/index.js
function mu(e) {
	return e.replace(/[\t\n\r ]+/g, " ").replace(/^ | $/g, "").toLowerCase().toUpperCase();
}
//#endregion
//#region frontend/node_modules/micromark-util-character/index.js
var hu = Eu(/[A-Za-z]/), gu = Eu(/[\dA-Za-z]/), _u = Eu(/[#-'*+\--9=?A-Z^-~]/);
function vu(e) {
	return e !== null && (e < 32 || e === 127);
}
var yu = Eu(/\d/), bu = Eu(/[\dA-Fa-f]/), xu = Eu(/[!-/:-@[-`{-~]/);
function $(e) {
	return e !== null && e < -2;
}
function Su(e) {
	return e !== null && (e < 0 || e === 32);
}
function Cu(e) {
	return e === -2 || e === -1 || e === 32;
}
var wu = Eu(/\p{P}|\p{S}/u), Tu = Eu(/\s/);
function Eu(e) {
	return t;
	function t(t) {
		return t !== null && t > -1 && e.test(String.fromCharCode(t));
	}
}
//#endregion
//#region frontend/node_modules/micromark-util-sanitize-uri/index.js
function Du(e) {
	let t = [], n = -1, r = 0, i = 0;
	for (; ++n < e.length;) {
		let a = e.charCodeAt(n), o = "";
		if (a === 37 && gu(e.charCodeAt(n + 1)) && gu(e.charCodeAt(n + 2))) i = 2;
		else if (a < 128) /[!#$&-;=?-Z_a-z~]/.test(String.fromCharCode(a)) || (o = String.fromCharCode(a));
		else if (a > 55295 && a < 57344) {
			let t = e.charCodeAt(n + 1);
			a < 56320 && t > 56319 && t < 57344 ? (o = String.fromCharCode(a, t), i = 1) : o = "�";
		} else o = String.fromCharCode(a);
		o &&= (t.push(e.slice(r, n), encodeURIComponent(o)), r = n + i + 1, ""), i &&= (n += i, 0);
	}
	return t.join("") + e.slice(r);
}
//#endregion
//#region frontend/node_modules/micromark-factory-space/index.js
function Ou(e, t, n, r) {
	let i = r ? r - 1 : Infinity, a = 0;
	return o;
	function o(r) {
		return Cu(r) ? (e.enter(n), s(r)) : t(r);
	}
	function s(r) {
		return Cu(r) && a++ < i ? (e.consume(r), s) : (e.exit(n), t(r));
	}
}
//#endregion
//#region frontend/node_modules/micromark/lib/initialize/content.js
var ku = { tokenize: Au };
function Au(e) {
	let t = e.attempt(this.parser.constructs.contentInitial, r, i), n;
	return t;
	function r(n) {
		if (n === null) {
			e.consume(n);
			return;
		}
		return e.enter("lineEnding"), e.consume(n), e.exit("lineEnding"), Ou(e, t, "linePrefix");
	}
	function i(t) {
		return e.enter("paragraph"), a(t);
	}
	function a(t) {
		let r = e.enter("chunkText", {
			contentType: "text",
			previous: n
		});
		return n && (n.next = r), n = r, o(t);
	}
	function o(t) {
		if (t === null) {
			e.exit("chunkText"), e.exit("paragraph"), e.consume(t);
			return;
		}
		return $(t) ? (e.consume(t), e.exit("chunkText"), a) : (e.consume(t), o);
	}
}
//#endregion
//#region frontend/node_modules/micromark/lib/initialize/document.js
var ju = { tokenize: Nu }, Mu = { tokenize: Pu };
function Nu(e) {
	let t = this, n = [], r = 0, i, a, o;
	return s;
	function s(i) {
		if (r < n.length) {
			let a = n[r];
			return t.containerState = a[1], e.attempt(a[0].continuation, c, l)(i);
		}
		return l(i);
	}
	function c(e) {
		if (r++, t.containerState._closeFlow) {
			t.containerState._closeFlow = void 0, i && v();
			let n = t.events.length, a = n, o;
			for (; a--;) if (t.events[a][0] === "exit" && t.events[a][1].type === "chunkFlow") {
				o = t.events[a][1].end;
				break;
			}
			_(r);
			let s = n;
			for (; s < t.events.length;) t.events[s][1].end = { ...o }, s++;
			return su(t.events, a + 1, 0, t.events.slice(n)), t.events.length = s, l(e);
		}
		return s(e);
	}
	function l(a) {
		if (r === n.length) {
			if (!i) return f(a);
			if (i.currentConstruct && i.currentConstruct.concrete) return m(a);
			t.interrupt = !!(i.currentConstruct && !i._gfmTableDynamicInterruptHack);
		}
		return t.containerState = {}, e.check(Mu, u, d)(a);
	}
	function u(e) {
		return i && v(), _(r), f(e);
	}
	function d(e) {
		return t.parser.lazy[t.now().line] = r !== n.length, o = t.now().offset, m(e);
	}
	function f(n) {
		return t.containerState = {}, e.attempt(Mu, p, m)(n);
	}
	function p(e) {
		return r++, n.push([t.currentConstruct, t.containerState]), f(e);
	}
	function m(n) {
		if (n === null) {
			i && v(), _(0), e.consume(n);
			return;
		}
		return i ||= t.parser.flow(t.now()), e.enter("chunkFlow", {
			_tokenizer: i,
			contentType: "flow",
			previous: a
		}), h(n);
	}
	function h(n) {
		if (n === null) {
			g(e.exit("chunkFlow"), !0), _(0), e.consume(n);
			return;
		}
		return $(n) ? (e.consume(n), g(e.exit("chunkFlow")), r = 0, t.interrupt = void 0, s) : (e.consume(n), h);
	}
	function g(e, n) {
		let s = t.sliceStream(e);
		if (n && s.push(null), e.previous = a, a && (a.next = e), a = e, i.defineSkip(e.start), i.write(s), t.parser.lazy[e.start.line]) {
			let e = i.events.length;
			for (; e--;) if (i.events[e][1].start.offset < o && (!i.events[e][1].end || i.events[e][1].end.offset > o)) return;
			let n = t.events.length, a = n, s, c;
			for (; a--;) if (t.events[a][0] === "exit" && t.events[a][1].type === "chunkFlow") {
				if (s) {
					c = t.events[a][1].end;
					break;
				}
				s = !0;
			}
			for (_(r), e = n; e < t.events.length;) t.events[e][1].end = { ...c }, e++;
			su(t.events, a + 1, 0, t.events.slice(n)), t.events.length = e;
		}
	}
	function _(r) {
		let i = n.length;
		for (; i-- > r;) {
			let r = n[i];
			t.containerState = r[1], r[0].exit.call(t, e);
		}
		n.length = r;
	}
	function v() {
		i.write([null]), a = void 0, i = void 0, t.containerState._closeFlow = void 0;
	}
}
function Pu(e, t, n) {
	return Ou(e, e.attempt(this.parser.constructs.document, t, n), "linePrefix", this.parser.constructs.disable.null.includes("codeIndented") ? void 0 : 4);
}
//#endregion
//#region frontend/node_modules/micromark-util-classify-character/index.js
function Fu(e) {
	if (e === null || Su(e) || Tu(e)) return 1;
	if (wu(e)) return 2;
}
//#endregion
//#region frontend/node_modules/micromark-util-resolve-all/index.js
function Iu(e, t, n) {
	let r = [], i = -1;
	for (; ++i < e.length;) {
		let a = e[i].resolveAll;
		a && !r.includes(a) && (t = a(t, n), r.push(a));
	}
	return t;
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/attention.js
var Lu = {
	name: "attention",
	resolveAll: Ru,
	tokenize: zu
};
function Ru(e, t) {
	let n = -1, r, i, a, o, s, c, l, u;
	for (; ++n < e.length;) if (e[n][0] === "enter" && e[n][1].type === "attentionSequence" && e[n][1]._close) {
		for (r = n; r--;) if (e[r][0] === "exit" && e[r][1].type === "attentionSequence" && e[r][1]._open && t.sliceSerialize(e[r][1]).charCodeAt(0) === t.sliceSerialize(e[n][1]).charCodeAt(0)) {
			if ((e[r][1]._close || e[n][1]._open) && (e[n][1].end.offset - e[n][1].start.offset) % 3 && !((e[r][1].end.offset - e[r][1].start.offset + e[n][1].end.offset - e[n][1].start.offset) % 3)) continue;
			c = e[r][1].end.offset - e[r][1].start.offset > 1 && e[n][1].end.offset - e[n][1].start.offset > 1 ? 2 : 1;
			let d = { ...e[r][1].end }, f = { ...e[n][1].start };
			Bu(d, -c), Bu(f, c), o = {
				type: c > 1 ? "strongSequence" : "emphasisSequence",
				start: d,
				end: { ...e[r][1].end }
			}, s = {
				type: c > 1 ? "strongSequence" : "emphasisSequence",
				start: { ...e[n][1].start },
				end: f
			}, a = {
				type: c > 1 ? "strongText" : "emphasisText",
				start: { ...e[r][1].end },
				end: { ...e[n][1].start }
			}, i = {
				type: c > 1 ? "strong" : "emphasis",
				start: { ...o.start },
				end: { ...s.end }
			}, e[r][1].end = { ...o.start }, e[n][1].start = { ...s.end }, l = [], e[r][1].end.offset - e[r][1].start.offset && (l = cu(l, [[
				"enter",
				e[r][1],
				t
			], [
				"exit",
				e[r][1],
				t
			]])), l = cu(l, [
				[
					"enter",
					i,
					t
				],
				[
					"enter",
					o,
					t
				],
				[
					"exit",
					o,
					t
				],
				[
					"enter",
					a,
					t
				]
			]), l = cu(l, Iu(t.parser.constructs.insideSpan.null, e.slice(r + 1, n), t)), l = cu(l, [
				[
					"exit",
					a,
					t
				],
				[
					"enter",
					s,
					t
				],
				[
					"exit",
					s,
					t
				],
				[
					"exit",
					i,
					t
				]
			]), e[n][1].end.offset - e[n][1].start.offset ? (u = 2, l = cu(l, [[
				"enter",
				e[n][1],
				t
			], [
				"exit",
				e[n][1],
				t
			]])) : u = 0, su(e, r - 1, n - r + 3, l), n = r + l.length - u - 2;
			break;
		}
	}
	for (n = -1; ++n < e.length;) e[n][1].type === "attentionSequence" && (e[n][1].type = "data");
	return e;
}
function zu(e, t) {
	let n = this.parser.constructs.attentionMarkers.null, r = this.previous, i = Fu(r), a;
	return o;
	function o(t) {
		return a = t, e.enter("attentionSequence"), s(t);
	}
	function s(o) {
		if (o === a) return e.consume(o), s;
		let c = e.exit("attentionSequence"), l = Fu(o), u = !l || l === 2 && i || n.includes(o), d = !i || i === 2 && l || n.includes(r);
		return c._open = !!(a === 42 ? u : u && (i || !d)), c._close = !!(a === 42 ? d : d && (l || !u)), t(o);
	}
}
function Bu(e, t) {
	e.column += t, e.offset += t, e._bufferIndex += t;
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/autolink.js
var Vu = {
	name: "autolink",
	tokenize: Hu
};
function Hu(e, t, n) {
	let r = 0;
	return i;
	function i(t) {
		return e.enter("autolink"), e.enter("autolinkMarker"), e.consume(t), e.exit("autolinkMarker"), e.enter("autolinkProtocol"), a;
	}
	function a(t) {
		return hu(t) ? (e.consume(t), o) : t === 64 ? n(t) : l(t);
	}
	function o(e) {
		return e === 43 || e === 45 || e === 46 || gu(e) ? (r = 1, s(e)) : l(e);
	}
	function s(t) {
		return t === 58 ? (e.consume(t), r = 0, c) : (t === 43 || t === 45 || t === 46 || gu(t)) && r++ < 32 ? (e.consume(t), s) : (r = 0, l(t));
	}
	function c(r) {
		return r === 62 ? (e.exit("autolinkProtocol"), e.enter("autolinkMarker"), e.consume(r), e.exit("autolinkMarker"), e.exit("autolink"), t) : r === null || r === 32 || r === 60 || vu(r) ? n(r) : (e.consume(r), c);
	}
	function l(t) {
		return t === 64 ? (e.consume(t), u) : _u(t) ? (e.consume(t), l) : n(t);
	}
	function u(e) {
		return gu(e) ? d(e) : n(e);
	}
	function d(n) {
		return n === 46 ? (e.consume(n), r = 0, u) : n === 62 ? (e.exit("autolinkProtocol").type = "autolinkEmail", e.enter("autolinkMarker"), e.consume(n), e.exit("autolinkMarker"), e.exit("autolink"), t) : f(n);
	}
	function f(t) {
		if ((t === 45 || gu(t)) && r++ < 63) {
			let n = t === 45 ? f : d;
			return e.consume(t), n;
		}
		return n(t);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/blank-line.js
var Uu = {
	partial: !0,
	tokenize: Wu
};
function Wu(e, t, n) {
	return r;
	function r(t) {
		return Cu(t) ? Ou(e, i, "linePrefix")(t) : i(t);
	}
	function i(e) {
		return e === null || $(e) ? t(e) : n(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/block-quote.js
var Gu = {
	continuation: { tokenize: qu },
	exit: Ju,
	name: "blockQuote",
	tokenize: Ku
};
function Ku(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		if (t === 62) {
			let n = r.containerState;
			return n.open ||= (e.enter("blockQuote", { _container: !0 }), !0), e.enter("blockQuotePrefix"), e.enter("blockQuoteMarker"), e.consume(t), e.exit("blockQuoteMarker"), a;
		}
		return n(t);
	}
	function a(n) {
		return Cu(n) ? (e.enter("blockQuotePrefixWhitespace"), e.consume(n), e.exit("blockQuotePrefixWhitespace"), e.exit("blockQuotePrefix"), t) : (e.exit("blockQuotePrefix"), t(n));
	}
}
function qu(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return Cu(t) ? Ou(e, a, "linePrefix", r.parser.constructs.disable.null.includes("codeIndented") ? void 0 : 4)(t) : a(t);
	}
	function a(r) {
		return e.attempt(Gu, t, n)(r);
	}
}
function Ju(e) {
	e.exit("blockQuote");
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/character-escape.js
var Yu = {
	name: "characterEscape",
	tokenize: Xu
};
function Xu(e, t, n) {
	return r;
	function r(t) {
		return e.enter("characterEscape"), e.enter("escapeMarker"), e.consume(t), e.exit("escapeMarker"), i;
	}
	function i(r) {
		return xu(r) ? (e.enter("characterEscapeValue"), e.consume(r), e.exit("characterEscapeValue"), e.exit("characterEscape"), t) : n(r);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/character-reference.js
var Zu = {
	name: "characterReference",
	tokenize: Qu
};
function Qu(e, t, n) {
	let r = this, i = 0, a, o;
	return s;
	function s(t) {
		return e.enter("characterReference"), e.enter("characterReferenceMarker"), e.consume(t), e.exit("characterReferenceMarker"), c;
	}
	function c(t) {
		return t === 35 ? (e.enter("characterReferenceMarkerNumeric"), e.consume(t), e.exit("characterReferenceMarkerNumeric"), l) : (e.enter("characterReferenceValue"), a = 31, o = gu, u(t));
	}
	function l(t) {
		return t === 88 || t === 120 ? (e.enter("characterReferenceMarkerHexadecimal"), e.consume(t), e.exit("characterReferenceMarkerHexadecimal"), e.enter("characterReferenceValue"), a = 6, o = bu, u) : (e.enter("characterReferenceValue"), a = 7, o = yu, u(t));
	}
	function u(s) {
		if (s === 59 && i) {
			let i = e.exit("characterReferenceValue");
			return o === gu && !ou(r.sliceSerialize(i)) ? n(s) : (e.enter("characterReferenceMarker"), e.consume(s), e.exit("characterReferenceMarker"), e.exit("characterReference"), t);
		}
		return o(s) && i++ < a ? (e.consume(s), u) : n(s);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/code-fenced.js
var $u = {
	partial: !0,
	tokenize: nd
}, ed = {
	concrete: !0,
	name: "codeFenced",
	tokenize: td
};
function td(e, t, n) {
	let r = this, i = {
		partial: !0,
		tokenize: x
	}, a = 0, o = 0, s;
	return c;
	function c(e) {
		return l(e);
	}
	function l(t) {
		let n = r.events[r.events.length - 1];
		return a = n && n[1].type === "linePrefix" ? n[2].sliceSerialize(n[1], !0).length : 0, s = t, e.enter("codeFenced"), e.enter("codeFencedFence"), e.enter("codeFencedFenceSequence"), u(t);
	}
	function u(t) {
		return t === s ? (o++, e.consume(t), u) : o < 3 ? n(t) : (e.exit("codeFencedFenceSequence"), Cu(t) ? Ou(e, d, "whitespace")(t) : d(t));
	}
	function d(n) {
		return n === null || $(n) ? (e.exit("codeFencedFence"), r.interrupt ? t(n) : e.check($u, h, b)(n)) : (e.enter("codeFencedFenceInfo"), e.enter("chunkString", { contentType: "string" }), f(n));
	}
	function f(t) {
		return t === null || $(t) ? (e.exit("chunkString"), e.exit("codeFencedFenceInfo"), d(t)) : Cu(t) ? (e.exit("chunkString"), e.exit("codeFencedFenceInfo"), Ou(e, p, "whitespace")(t)) : t === 96 && t === s ? n(t) : (e.consume(t), f);
	}
	function p(t) {
		return t === null || $(t) ? d(t) : (e.enter("codeFencedFenceMeta"), e.enter("chunkString", { contentType: "string" }), m(t));
	}
	function m(t) {
		return t === null || $(t) ? (e.exit("chunkString"), e.exit("codeFencedFenceMeta"), d(t)) : t === 96 && t === s ? n(t) : (e.consume(t), m);
	}
	function h(t) {
		return e.attempt(i, b, g)(t);
	}
	function g(t) {
		return e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), _;
	}
	function _(t) {
		return a > 0 && Cu(t) ? Ou(e, v, "linePrefix", a + 1)(t) : v(t);
	}
	function v(t) {
		return t === null || $(t) ? e.check($u, h, b)(t) : (e.enter("codeFlowValue"), y(t));
	}
	function y(t) {
		return t === null || $(t) ? (e.exit("codeFlowValue"), v(t)) : (e.consume(t), y);
	}
	function b(n) {
		return e.exit("codeFenced"), t(n);
	}
	function x(e, t, n) {
		let i = 0;
		return a;
		function a(t) {
			return e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), c;
		}
		function c(t) {
			return e.enter("codeFencedFence"), Cu(t) ? Ou(e, l, "linePrefix", r.parser.constructs.disable.null.includes("codeIndented") ? void 0 : 4)(t) : l(t);
		}
		function l(t) {
			return t === s ? (e.enter("codeFencedFenceSequence"), u(t)) : n(t);
		}
		function u(t) {
			return t === s ? (i++, e.consume(t), u) : i >= o ? (e.exit("codeFencedFenceSequence"), Cu(t) ? Ou(e, d, "whitespace")(t) : d(t)) : n(t);
		}
		function d(r) {
			return r === null || $(r) ? (e.exit("codeFencedFence"), t(r)) : n(r);
		}
	}
}
function nd(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return t === null ? n(t) : (e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), a);
	}
	function a(e) {
		return r.parser.lazy[r.now().line] ? n(e) : t(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/code-indented.js
var rd = {
	name: "codeIndented",
	tokenize: ad
}, id = {
	partial: !0,
	tokenize: od
};
function ad(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return e.enter("codeIndented"), Ou(e, a, "linePrefix", 5)(t);
	}
	function a(e) {
		let t = r.events[r.events.length - 1];
		return t && t[1].type === "linePrefix" && t[2].sliceSerialize(t[1], !0).length >= 4 ? o(e) : n(e);
	}
	function o(t) {
		return t === null ? c(t) : $(t) ? e.attempt(id, o, c)(t) : (e.enter("codeFlowValue"), s(t));
	}
	function s(t) {
		return t === null || $(t) ? (e.exit("codeFlowValue"), o(t)) : (e.consume(t), s);
	}
	function c(n) {
		return e.exit("codeIndented"), t(n);
	}
}
function od(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return r.parser.lazy[r.now().line] ? n(t) : $(t) ? (e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), i) : Ou(e, a, "linePrefix", 5)(t);
	}
	function a(e) {
		let a = r.events[r.events.length - 1];
		return a && a[1].type === "linePrefix" && a[2].sliceSerialize(a[1], !0).length >= 4 ? t(e) : $(e) ? i(e) : n(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/code-text.js
var sd = {
	name: "codeText",
	previous: ld,
	resolve: cd,
	tokenize: ud
};
function cd(e) {
	let t = e.length - 4, n = 3, r, i;
	if ((e[n][1].type === "lineEnding" || e[n][1].type === "space") && (e[t][1].type === "lineEnding" || e[t][1].type === "space")) {
		for (r = n; ++r < t;) if (e[r][1].type === "codeTextData") {
			e[n][1].type = "codeTextPadding", e[t][1].type = "codeTextPadding", n += 2, t -= 2;
			break;
		}
	}
	for (r = n - 1, t++; ++r <= t;) i === void 0 ? r !== t && e[r][1].type !== "lineEnding" && (i = r) : (r === t || e[r][1].type === "lineEnding") && (e[i][1].type = "codeTextData", r !== i + 2 && (e[i][1].end = e[r - 1][1].end, e.splice(i + 2, r - i - 2), t -= r - i - 2, r = i + 2), i = void 0);
	return e;
}
function ld(e) {
	return e !== 96 || this.events[this.events.length - 1][1].type === "characterEscape";
}
function ud(e, t, n) {
	let r = 0, i, a;
	return o;
	function o(t) {
		return e.enter("codeText"), e.enter("codeTextSequence"), s(t);
	}
	function s(t) {
		return t === 96 ? (e.consume(t), r++, s) : (e.exit("codeTextSequence"), c(t));
	}
	function c(t) {
		return t === null ? n(t) : t === 32 ? (e.enter("space"), e.consume(t), e.exit("space"), c) : t === 96 ? (a = e.enter("codeTextSequence"), i = 0, u(t)) : $(t) ? (e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), c) : (e.enter("codeTextData"), l(t));
	}
	function l(t) {
		return t === null || t === 32 || t === 96 || $(t) ? (e.exit("codeTextData"), c(t)) : (e.consume(t), l);
	}
	function u(n) {
		return n === 96 ? (e.consume(n), i++, u) : i === r ? (e.exit("codeTextSequence"), e.exit("codeText"), t(n)) : (a.type = "codeTextData", l(n));
	}
}
//#endregion
//#region frontend/node_modules/micromark-util-subtokenize/lib/splice-buffer.js
var dd = class {
	constructor(e) {
		this.left = e ? [...e] : [], this.right = [];
	}
	get(e) {
		if (e < 0 || e >= this.left.length + this.right.length) throw RangeError("Cannot access index `" + e + "` in a splice buffer of size `" + (this.left.length + this.right.length) + "`");
		return e < this.left.length ? this.left[e] : this.right[this.right.length - e + this.left.length - 1];
	}
	get length() {
		return this.left.length + this.right.length;
	}
	shift() {
		return this.setCursor(0), this.right.pop();
	}
	slice(e, t) {
		let n = t ?? Infinity;
		return n < this.left.length ? this.left.slice(e, n) : e > this.left.length ? this.right.slice(this.right.length - n + this.left.length, this.right.length - e + this.left.length).reverse() : this.left.slice(e).concat(this.right.slice(this.right.length - n + this.left.length).reverse());
	}
	splice(e, t, n) {
		let r = t || 0;
		this.setCursor(Math.trunc(e));
		let i = this.right.splice(this.right.length - r, Infinity);
		return n && fd(this.left, n), i.reverse();
	}
	pop() {
		return this.setCursor(Infinity), this.left.pop();
	}
	push(e) {
		this.setCursor(Infinity), this.left.push(e);
	}
	pushMany(e) {
		this.setCursor(Infinity), fd(this.left, e);
	}
	unshift(e) {
		this.setCursor(0), this.right.push(e);
	}
	unshiftMany(e) {
		this.setCursor(0), fd(this.right, e.reverse());
	}
	setCursor(e) {
		if (!(e === this.left.length || e > this.left.length && this.right.length === 0 || e < 0 && this.left.length === 0)) if (e < this.left.length) {
			let t = this.left.splice(e, Infinity);
			fd(this.right, t.reverse());
		} else {
			let t = this.right.splice(this.left.length + this.right.length - e, Infinity);
			fd(this.left, t.reverse());
		}
	}
};
function fd(e, t) {
	let n = 0;
	if (t.length < 1e4) e.push(...t);
	else for (; n < t.length;) e.push(...t.slice(n, n + 1e4)), n += 1e4;
}
//#endregion
//#region frontend/node_modules/micromark-util-subtokenize/index.js
function pd(e) {
	let t = {}, n = -1, r, i, a, o, s, c, l, u = new dd(e);
	for (; ++n < u.length;) {
		for (; n in t;) n = t[n];
		if (r = u.get(n), n && r[1].type === "chunkFlow" && u.get(n - 1)[1].type === "listItemPrefix" && (c = r[1]._tokenizer.events, a = 0, a < c.length && c[a][1].type === "lineEndingBlank" && (a += 2), a < c.length && c[a][1].type === "content")) for (; ++a < c.length && c[a][1].type !== "content";) c[a][1].type === "chunkText" && (c[a][1]._isInFirstContentOfListItem = !0, a++);
		if (r[0] === "enter") r[1].contentType && (Object.assign(t, md(u, n)), n = t[n], l = !0);
		else if (r[1]._container) {
			for (a = n, i = void 0; a--;) if (o = u.get(a), o[1].type === "lineEnding" || o[1].type === "lineEndingBlank") o[0] === "enter" && (i && (u.get(i)[1].type = "lineEndingBlank"), o[1].type = "lineEnding", i = a);
			else if (!(o[1].type === "linePrefix" || o[1].type === "listItemIndent")) break;
			i && (r[1].end = { ...u.get(i)[1].start }, s = u.slice(i, n), s.unshift(r), u.splice(i, n - i + 1, s));
		}
	}
	return su(e, 0, Infinity, u.slice(0)), !l;
}
function md(e, t) {
	let n = e.get(t)[1], r = e.get(t)[2], i = t - 1, a = [], o = n._tokenizer;
	o || (o = r.parser[n.contentType](n.start), n._contentTypeTextTrailing && (o._contentTypeTextTrailing = !0));
	let s = o.events, c = [], l = {}, u, d, f = -1, p = n, m = 0, h = 0, g = [h];
	for (; p;) {
		for (; e.get(++i)[1] !== p;);
		a.push(i), p._tokenizer || (u = r.sliceStream(p), p.next || u.push(null), d && o.defineSkip(p.start), p._isInFirstContentOfListItem && (o._gfmTasklistFirstContentOfListItem = !0), o.write(u), p._isInFirstContentOfListItem && (o._gfmTasklistFirstContentOfListItem = void 0)), d = p, p = p.next;
	}
	for (p = n; ++f < s.length;) s[f][0] === "exit" && s[f - 1][0] === "enter" && s[f][1].type === s[f - 1][1].type && s[f][1].start.line !== s[f][1].end.line && (h = f + 1, g.push(h), p._tokenizer = void 0, p.previous = void 0, p = p.next);
	for (o.events = [], p ? (p._tokenizer = void 0, p.previous = void 0) : g.pop(), f = g.length; f--;) {
		let t = s.slice(g[f], g[f + 1]), n = a.pop();
		c.push([n, n + t.length - 1]), e.splice(n, 2, t);
	}
	for (c.reverse(), f = -1; ++f < c.length;) l[m + c[f][0]] = m + c[f][1], m += c[f][1] - c[f][0] - 1;
	return l;
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/content.js
var hd = {
	resolve: _d,
	tokenize: vd
}, gd = {
	partial: !0,
	tokenize: yd
};
function _d(e) {
	return pd(e), e;
}
function vd(e, t) {
	let n;
	return r;
	function r(t) {
		return e.enter("content"), n = e.enter("chunkContent", { contentType: "content" }), i(t);
	}
	function i(t) {
		return t === null ? a(t) : $(t) ? e.check(gd, o, a)(t) : (e.consume(t), i);
	}
	function a(n) {
		return e.exit("chunkContent"), e.exit("content"), t(n);
	}
	function o(t) {
		return e.consume(t), e.exit("chunkContent"), n.next = e.enter("chunkContent", {
			contentType: "content",
			previous: n
		}), n = n.next, i;
	}
}
function yd(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return e.exit("chunkContent"), e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), Ou(e, a, "linePrefix");
	}
	function a(i) {
		if (i === null || $(i)) return n(i);
		let a = r.events[r.events.length - 1];
		return !r.parser.constructs.disable.null.includes("codeIndented") && a && a[1].type === "linePrefix" && a[2].sliceSerialize(a[1], !0).length >= 4 ? t(i) : e.interrupt(r.parser.constructs.flow, n, t)(i);
	}
}
//#endregion
//#region frontend/node_modules/micromark-factory-destination/index.js
function bd(e, t, n, r, i, a, o, s, c) {
	let l = c || Infinity, u = 0;
	return d;
	function d(t) {
		return t === 60 ? (e.enter(r), e.enter(i), e.enter(a), e.consume(t), e.exit(a), f) : t === null || t === 32 || t === 41 || vu(t) ? n(t) : (e.enter(r), e.enter(o), e.enter(s), e.enter("chunkString", { contentType: "string" }), h(t));
	}
	function f(n) {
		return n === 62 ? (e.enter(a), e.consume(n), e.exit(a), e.exit(i), e.exit(r), t) : (e.enter(s), e.enter("chunkString", { contentType: "string" }), p(n));
	}
	function p(t) {
		return t === 62 ? (e.exit("chunkString"), e.exit(s), f(t)) : t === null || t === 60 || $(t) ? n(t) : (e.consume(t), t === 92 ? m : p);
	}
	function m(t) {
		return t === 60 || t === 62 || t === 92 ? (e.consume(t), p) : p(t);
	}
	function h(i) {
		return !u && (i === null || i === 41 || Su(i)) ? (e.exit("chunkString"), e.exit(s), e.exit(o), e.exit(r), t(i)) : u < l && i === 40 ? (e.consume(i), u++, h) : i === 41 ? (e.consume(i), u--, h) : i === null || i === 32 || i === 40 || vu(i) ? n(i) : (e.consume(i), i === 92 ? g : h);
	}
	function g(t) {
		return t === 40 || t === 41 || t === 92 ? (e.consume(t), h) : h(t);
	}
}
//#endregion
//#region frontend/node_modules/micromark-factory-label/index.js
function xd(e, t, n, r, i, a) {
	let o = this, s = 0, c;
	return l;
	function l(t) {
		return e.enter(r), e.enter(i), e.consume(t), e.exit(i), e.enter(a), u;
	}
	function u(l) {
		return s > 999 || l === null || l === 91 || l === 93 && !c || l === 94 && !s && "_hiddenFootnoteSupport" in o.parser.constructs ? n(l) : l === 93 ? (e.exit(a), e.enter(i), e.consume(l), e.exit(i), e.exit(r), t) : $(l) ? (e.enter("lineEnding"), e.consume(l), e.exit("lineEnding"), u) : (e.enter("chunkString", { contentType: "string" }), d(l));
	}
	function d(t) {
		return t === null || t === 91 || t === 93 || $(t) || s++ > 999 ? (e.exit("chunkString"), u(t)) : (e.consume(t), c ||= !Cu(t), t === 92 ? f : d);
	}
	function f(t) {
		return t === 91 || t === 92 || t === 93 ? (e.consume(t), s++, d) : d(t);
	}
}
//#endregion
//#region frontend/node_modules/micromark-factory-title/index.js
function Sd(e, t, n, r, i, a) {
	let o;
	return s;
	function s(t) {
		return t === 34 || t === 39 || t === 40 ? (e.enter(r), e.enter(i), e.consume(t), e.exit(i), o = t === 40 ? 41 : t, c) : n(t);
	}
	function c(n) {
		return n === o ? (e.enter(i), e.consume(n), e.exit(i), e.exit(r), t) : (e.enter(a), l(n));
	}
	function l(t) {
		return t === o ? (e.exit(a), c(o)) : t === null ? n(t) : $(t) ? (e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), Ou(e, l, "linePrefix")) : (e.enter("chunkString", { contentType: "string" }), u(t));
	}
	function u(t) {
		return t === o || t === null || $(t) ? (e.exit("chunkString"), l(t)) : (e.consume(t), t === 92 ? d : u);
	}
	function d(t) {
		return t === o || t === 92 ? (e.consume(t), u) : u(t);
	}
}
//#endregion
//#region frontend/node_modules/micromark-factory-whitespace/index.js
function Cd(e, t) {
	let n;
	return r;
	function r(i) {
		return $(i) ? (e.enter("lineEnding"), e.consume(i), e.exit("lineEnding"), n = !0, r) : Cu(i) ? Ou(e, r, n ? "linePrefix" : "lineSuffix")(i) : t(i);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/definition.js
var wd = {
	name: "definition",
	tokenize: Ed
}, Td = {
	partial: !0,
	tokenize: Dd
};
function Ed(e, t, n) {
	let r = this, i;
	return a;
	function a(t) {
		return e.enter("definition"), o(t);
	}
	function o(t) {
		return xd.call(r, e, s, n, "definitionLabel", "definitionLabelMarker", "definitionLabelString")(t);
	}
	function s(t) {
		return i = mu(r.sliceSerialize(r.events[r.events.length - 1][1]).slice(1, -1)), t === 58 ? (e.enter("definitionMarker"), e.consume(t), e.exit("definitionMarker"), c) : n(t);
	}
	function c(t) {
		return Su(t) ? Cd(e, l)(t) : l(t);
	}
	function l(t) {
		return bd(e, u, n, "definitionDestination", "definitionDestinationLiteral", "definitionDestinationLiteralMarker", "definitionDestinationRaw", "definitionDestinationString")(t);
	}
	function u(t) {
		return e.attempt(Td, d, d)(t);
	}
	function d(t) {
		return Cu(t) ? Ou(e, f, "whitespace")(t) : f(t);
	}
	function f(a) {
		return a === null || $(a) ? (e.exit("definition"), r.parser.defined.push(i), t(a)) : n(a);
	}
}
function Dd(e, t, n) {
	return r;
	function r(t) {
		return Su(t) ? Cd(e, i)(t) : n(t);
	}
	function i(t) {
		return Sd(e, a, n, "definitionTitle", "definitionTitleMarker", "definitionTitleString")(t);
	}
	function a(t) {
		return Cu(t) ? Ou(e, o, "whitespace")(t) : o(t);
	}
	function o(e) {
		return e === null || $(e) ? t(e) : n(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/hard-break-escape.js
var Od = {
	name: "hardBreakEscape",
	tokenize: kd
};
function kd(e, t, n) {
	return r;
	function r(t) {
		return e.enter("hardBreakEscape"), e.consume(t), i;
	}
	function i(r) {
		return $(r) ? (e.exit("hardBreakEscape"), t(r)) : n(r);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/heading-atx.js
var Ad = {
	name: "headingAtx",
	resolve: jd,
	tokenize: Md
};
function jd(e, t) {
	let n = e.length - 2, r = 3, i, a;
	return e[r][1].type === "whitespace" && (r += 2), n - 2 > r && e[n][1].type === "whitespace" && (n -= 2), e[n][1].type === "atxHeadingSequence" && (r === n - 1 || n - 4 > r && e[n - 2][1].type === "whitespace") && (n -= r + 1 === n ? 2 : 4), n > r && (i = {
		type: "atxHeadingText",
		start: e[r][1].start,
		end: e[n][1].end
	}, a = {
		type: "chunkText",
		start: e[r][1].start,
		end: e[n][1].end,
		contentType: "text"
	}, su(e, r, n - r + 1, [
		[
			"enter",
			i,
			t
		],
		[
			"enter",
			a,
			t
		],
		[
			"exit",
			a,
			t
		],
		[
			"exit",
			i,
			t
		]
	])), e;
}
function Md(e, t, n) {
	let r = 0;
	return i;
	function i(t) {
		return e.enter("atxHeading"), a(t);
	}
	function a(t) {
		return e.enter("atxHeadingSequence"), o(t);
	}
	function o(t) {
		return t === 35 && r++ < 6 ? (e.consume(t), o) : t === null || Su(t) ? (e.exit("atxHeadingSequence"), s(t)) : n(t);
	}
	function s(n) {
		return n === 35 ? (e.enter("atxHeadingSequence"), c(n)) : n === null || $(n) ? (e.exit("atxHeading"), t(n)) : Cu(n) ? Ou(e, s, "whitespace")(n) : (e.enter("atxHeadingText"), l(n));
	}
	function c(t) {
		return t === 35 ? (e.consume(t), c) : (e.exit("atxHeadingSequence"), s(t));
	}
	function l(t) {
		return t === null || t === 35 || Su(t) ? (e.exit("atxHeadingText"), s(t)) : (e.consume(t), l);
	}
}
//#endregion
//#region frontend/node_modules/micromark-util-html-tag-name/index.js
var Nd = /* @__PURE__ */ "address.article.aside.base.basefont.blockquote.body.caption.center.col.colgroup.dd.details.dialog.dir.div.dl.dt.fieldset.figcaption.figure.footer.form.frame.frameset.h1.h2.h3.h4.h5.h6.head.header.hr.html.iframe.legend.li.link.main.menu.menuitem.nav.noframes.ol.optgroup.option.p.param.search.section.summary.table.tbody.td.tfoot.th.thead.title.tr.track.ul".split("."), Pd = [
	"pre",
	"script",
	"style",
	"textarea"
], Fd = {
	concrete: !0,
	name: "htmlFlow",
	resolveTo: Rd,
	tokenize: zd
}, Id = {
	partial: !0,
	tokenize: Vd
}, Ld = {
	partial: !0,
	tokenize: Bd
};
function Rd(e) {
	let t = e.length;
	for (; t-- && !(e[t][0] === "enter" && e[t][1].type === "htmlFlow"););
	return t > 1 && e[t - 2][1].type === "linePrefix" && (e[t][1].start = e[t - 2][1].start, e[t + 1][1].start = e[t - 2][1].start, e.splice(t - 2, 2)), e;
}
function zd(e, t, n) {
	let r = this, i, a, o, s, c;
	return l;
	function l(e) {
		return u(e);
	}
	function u(t) {
		return e.enter("htmlFlow"), e.enter("htmlFlowData"), e.consume(t), d;
	}
	function d(s) {
		return s === 33 ? (e.consume(s), f) : s === 47 ? (e.consume(s), a = !0, h) : s === 63 ? (e.consume(s), i = 3, r.interrupt ? t : O) : hu(s) ? (e.consume(s), o = String.fromCharCode(s), g) : n(s);
	}
	function f(a) {
		return a === 45 ? (e.consume(a), i = 2, p) : a === 91 ? (e.consume(a), i = 5, s = 0, m) : hu(a) ? (e.consume(a), i = 4, r.interrupt ? t : O) : n(a);
	}
	function p(i) {
		return i === 45 ? (e.consume(i), r.interrupt ? t : O) : n(i);
	}
	function m(i) {
		return i === "CDATA[".charCodeAt(s++) ? (e.consume(i), s === 6 ? r.interrupt ? t : D : m) : n(i);
	}
	function h(t) {
		return hu(t) ? (e.consume(t), o = String.fromCharCode(t), g) : n(t);
	}
	function g(s) {
		if (s === null || s === 47 || s === 62 || Su(s)) {
			let c = s === 47, l = o.toLowerCase();
			return !c && !a && Pd.includes(l) ? (i = 1, r.interrupt ? t(s) : D(s)) : Nd.includes(o.toLowerCase()) ? (i = 6, c ? (e.consume(s), _) : r.interrupt ? t(s) : D(s)) : (i = 7, r.interrupt && !r.parser.lazy[r.now().line] ? n(s) : a ? v(s) : y(s));
		}
		return s === 45 || gu(s) ? (e.consume(s), o += String.fromCharCode(s), g) : n(s);
	}
	function _(i) {
		return i === 62 ? (e.consume(i), r.interrupt ? t : D) : n(i);
	}
	function v(t) {
		return Cu(t) ? (e.consume(t), v) : E(t);
	}
	function y(t) {
		return t === 47 ? (e.consume(t), E) : t === 58 || t === 95 || hu(t) ? (e.consume(t), b) : Cu(t) ? (e.consume(t), y) : E(t);
	}
	function b(t) {
		return t === 45 || t === 46 || t === 58 || t === 95 || gu(t) ? (e.consume(t), b) : x(t);
	}
	function x(t) {
		return t === 61 ? (e.consume(t), S) : Cu(t) ? (e.consume(t), x) : y(t);
	}
	function S(t) {
		return t === null || t === 60 || t === 61 || t === 62 || t === 96 ? n(t) : t === 34 || t === 39 ? (e.consume(t), c = t, C) : Cu(t) ? (e.consume(t), S) : w(t);
	}
	function C(t) {
		return t === c ? (e.consume(t), c = null, T) : t === null || $(t) ? n(t) : (e.consume(t), C);
	}
	function w(t) {
		return t === null || t === 34 || t === 39 || t === 47 || t === 60 || t === 61 || t === 62 || t === 96 || Su(t) ? x(t) : (e.consume(t), w);
	}
	function T(e) {
		return e === 47 || e === 62 || Cu(e) ? y(e) : n(e);
	}
	function E(t) {
		return t === 62 ? (e.consume(t), ee) : n(t);
	}
	function ee(t) {
		return t === null || $(t) ? D(t) : Cu(t) ? (e.consume(t), ee) : n(t);
	}
	function D(t) {
		return t === 45 && i === 2 ? (e.consume(t), ie) : t === 60 && i === 1 ? (e.consume(t), ae) : t === 62 && i === 4 ? (e.consume(t), k) : t === 63 && i === 3 ? (e.consume(t), O) : t === 93 && i === 5 ? (e.consume(t), se) : $(t) && (i === 6 || i === 7) ? (e.exit("htmlFlowData"), e.check(Id, ce, te)(t)) : t === null || $(t) ? (e.exit("htmlFlowData"), te(t)) : (e.consume(t), D);
	}
	function te(t) {
		return e.check(Ld, ne, ce)(t);
	}
	function ne(t) {
		return e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), re;
	}
	function re(t) {
		return t === null || $(t) ? te(t) : (e.enter("htmlFlowData"), D(t));
	}
	function ie(t) {
		return t === 45 ? (e.consume(t), O) : D(t);
	}
	function ae(t) {
		return t === 47 ? (e.consume(t), o = "", oe) : D(t);
	}
	function oe(t) {
		if (t === 62) {
			let n = o.toLowerCase();
			return Pd.includes(n) ? (e.consume(t), k) : D(t);
		}
		return hu(t) && o.length < 8 ? (e.consume(t), o += String.fromCharCode(t), oe) : D(t);
	}
	function se(t) {
		return t === 93 ? (e.consume(t), O) : D(t);
	}
	function O(t) {
		return t === 62 ? (e.consume(t), k) : t === 45 && i === 2 ? (e.consume(t), O) : D(t);
	}
	function k(t) {
		return t === null || $(t) ? (e.exit("htmlFlowData"), ce(t)) : (e.consume(t), k);
	}
	function ce(n) {
		return e.exit("htmlFlow"), t(n);
	}
}
function Bd(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return $(t) ? (e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), a) : n(t);
	}
	function a(e) {
		return r.parser.lazy[r.now().line] ? n(e) : t(e);
	}
}
function Vd(e, t, n) {
	return r;
	function r(r) {
		return e.enter("lineEnding"), e.consume(r), e.exit("lineEnding"), e.attempt(Uu, t, n);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/html-text.js
var Hd = {
	name: "htmlText",
	tokenize: Ud
};
function Ud(e, t, n) {
	let r = this, i, a, o;
	return s;
	function s(t) {
		return e.enter("htmlText"), e.enter("htmlTextData"), e.consume(t), c;
	}
	function c(t) {
		return t === 33 ? (e.consume(t), l) : t === 47 ? (e.consume(t), x) : t === 63 ? (e.consume(t), y) : hu(t) ? (e.consume(t), w) : n(t);
	}
	function l(t) {
		return t === 45 ? (e.consume(t), u) : t === 91 ? (e.consume(t), a = 0, m) : hu(t) ? (e.consume(t), v) : n(t);
	}
	function u(t) {
		return t === 45 ? (e.consume(t), p) : n(t);
	}
	function d(t) {
		return t === null ? n(t) : t === 45 ? (e.consume(t), f) : $(t) ? (o = d, ae(t)) : (e.consume(t), d);
	}
	function f(t) {
		return t === 45 ? (e.consume(t), p) : d(t);
	}
	function p(e) {
		return e === 62 ? ie(e) : e === 45 ? f(e) : d(e);
	}
	function m(t) {
		return t === "CDATA[".charCodeAt(a++) ? (e.consume(t), a === 6 ? h : m) : n(t);
	}
	function h(t) {
		return t === null ? n(t) : t === 93 ? (e.consume(t), g) : $(t) ? (o = h, ae(t)) : (e.consume(t), h);
	}
	function g(t) {
		return t === 93 ? (e.consume(t), _) : h(t);
	}
	function _(t) {
		return t === 62 ? ie(t) : t === 93 ? (e.consume(t), _) : h(t);
	}
	function v(t) {
		return t === null || t === 62 ? ie(t) : $(t) ? (o = v, ae(t)) : (e.consume(t), v);
	}
	function y(t) {
		return t === null ? n(t) : t === 63 ? (e.consume(t), b) : $(t) ? (o = y, ae(t)) : (e.consume(t), y);
	}
	function b(e) {
		return e === 62 ? ie(e) : y(e);
	}
	function x(t) {
		return hu(t) ? (e.consume(t), S) : n(t);
	}
	function S(t) {
		return t === 45 || gu(t) ? (e.consume(t), S) : C(t);
	}
	function C(t) {
		return $(t) ? (o = C, ae(t)) : Cu(t) ? (e.consume(t), C) : ie(t);
	}
	function w(t) {
		return t === 45 || gu(t) ? (e.consume(t), w) : t === 47 || t === 62 || Su(t) ? T(t) : n(t);
	}
	function T(t) {
		return t === 47 ? (e.consume(t), ie) : t === 58 || t === 95 || hu(t) ? (e.consume(t), E) : $(t) ? (o = T, ae(t)) : Cu(t) ? (e.consume(t), T) : ie(t);
	}
	function E(t) {
		return t === 45 || t === 46 || t === 58 || t === 95 || gu(t) ? (e.consume(t), E) : ee(t);
	}
	function ee(t) {
		return t === 61 ? (e.consume(t), D) : $(t) ? (o = ee, ae(t)) : Cu(t) ? (e.consume(t), ee) : T(t);
	}
	function D(t) {
		return t === null || t === 60 || t === 61 || t === 62 || t === 96 ? n(t) : t === 34 || t === 39 ? (e.consume(t), i = t, te) : $(t) ? (o = D, ae(t)) : Cu(t) ? (e.consume(t), D) : (e.consume(t), ne);
	}
	function te(t) {
		return t === i ? (e.consume(t), i = void 0, re) : t === null ? n(t) : $(t) ? (o = te, ae(t)) : (e.consume(t), te);
	}
	function ne(t) {
		return t === null || t === 34 || t === 39 || t === 60 || t === 61 || t === 96 ? n(t) : t === 47 || t === 62 || Su(t) ? T(t) : (e.consume(t), ne);
	}
	function re(e) {
		return e === 47 || e === 62 || Su(e) ? T(e) : n(e);
	}
	function ie(r) {
		return r === 62 ? (e.consume(r), e.exit("htmlTextData"), e.exit("htmlText"), t) : n(r);
	}
	function ae(t) {
		return e.exit("htmlTextData"), e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), oe;
	}
	function oe(t) {
		return Cu(t) ? Ou(e, se, "linePrefix", r.parser.constructs.disable.null.includes("codeIndented") ? void 0 : 4)(t) : se(t);
	}
	function se(t) {
		return e.enter("htmlTextData"), o(t);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/label-end.js
var Wd = {
	name: "labelEnd",
	resolveAll: Jd,
	resolveTo: Yd,
	tokenize: Xd
}, Gd = { tokenize: Zd }, Kd = { tokenize: Qd }, qd = { tokenize: $d };
function Jd(e) {
	let t = -1, n = [];
	for (; ++t < e.length;) {
		let r = e[t][1];
		if (n.push(e[t]), r.type === "labelImage" || r.type === "labelLink" || r.type === "labelEnd") {
			let e = r.type === "labelImage" ? 4 : 2;
			r.type = "data", t += e;
		}
	}
	return e.length !== n.length && su(e, 0, e.length, n), e;
}
function Yd(e, t) {
	let n = e.length, r = 0, i, a, o, s;
	for (; n--;) if (i = e[n][1], a) {
		if (i.type === "link" || i.type === "labelLink" && i._inactive) break;
		e[n][0] === "enter" && i.type === "labelLink" && (i._inactive = !0);
	} else if (o) {
		if (e[n][0] === "enter" && (i.type === "labelImage" || i.type === "labelLink") && !i._balanced && (a = n, i.type !== "labelLink")) {
			r = 2;
			break;
		}
	} else i.type === "labelEnd" && (o = n);
	let c = {
		type: e[a][1].type === "labelLink" ? "link" : "image",
		start: { ...e[a][1].start },
		end: { ...e[e.length - 1][1].end }
	}, l = {
		type: "label",
		start: { ...e[a][1].start },
		end: { ...e[o][1].end }
	}, u = {
		type: "labelText",
		start: { ...e[a + r + 2][1].end },
		end: { ...e[o - 2][1].start }
	};
	return s = [[
		"enter",
		c,
		t
	], [
		"enter",
		l,
		t
	]], s = cu(s, e.slice(a + 1, a + r + 3)), s = cu(s, [[
		"enter",
		u,
		t
	]]), s = cu(s, Iu(t.parser.constructs.insideSpan.null, e.slice(a + r + 4, o - 3), t)), s = cu(s, [
		[
			"exit",
			u,
			t
		],
		e[o - 2],
		e[o - 1],
		[
			"exit",
			l,
			t
		]
	]), s = cu(s, e.slice(o + 1)), s = cu(s, [[
		"exit",
		c,
		t
	]]), su(e, a, e.length, s), e;
}
function Xd(e, t, n) {
	let r = this, i = r.events.length, a, o;
	for (; i--;) if ((r.events[i][1].type === "labelImage" || r.events[i][1].type === "labelLink") && !r.events[i][1]._balanced) {
		a = r.events[i][1];
		break;
	}
	return s;
	function s(t) {
		return a ? a._inactive ? d(t) : (o = r.parser.defined.includes(mu(r.sliceSerialize({
			start: a.end,
			end: r.now()
		}))), e.enter("labelEnd"), e.enter("labelMarker"), e.consume(t), e.exit("labelMarker"), e.exit("labelEnd"), c) : n(t);
	}
	function c(t) {
		return t === 40 ? e.attempt(Gd, u, o ? u : d)(t) : t === 91 ? e.attempt(Kd, u, o ? l : d)(t) : o ? u(t) : d(t);
	}
	function l(t) {
		return e.attempt(qd, u, d)(t);
	}
	function u(e) {
		return t(e);
	}
	function d(e) {
		return a._balanced = !0, n(e);
	}
}
function Zd(e, t, n) {
	return r;
	function r(t) {
		return e.enter("resource"), e.enter("resourceMarker"), e.consume(t), e.exit("resourceMarker"), i;
	}
	function i(t) {
		return Su(t) ? Cd(e, a)(t) : a(t);
	}
	function a(t) {
		return t === 41 ? u(t) : bd(e, o, s, "resourceDestination", "resourceDestinationLiteral", "resourceDestinationLiteralMarker", "resourceDestinationRaw", "resourceDestinationString", 32)(t);
	}
	function o(t) {
		return Su(t) ? Cd(e, c)(t) : u(t);
	}
	function s(e) {
		return n(e);
	}
	function c(t) {
		return t === 34 || t === 39 || t === 40 ? Sd(e, l, n, "resourceTitle", "resourceTitleMarker", "resourceTitleString")(t) : u(t);
	}
	function l(t) {
		return Su(t) ? Cd(e, u)(t) : u(t);
	}
	function u(r) {
		return r === 41 ? (e.enter("resourceMarker"), e.consume(r), e.exit("resourceMarker"), e.exit("resource"), t) : n(r);
	}
}
function Qd(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return xd.call(r, e, a, o, "reference", "referenceMarker", "referenceString")(t);
	}
	function a(e) {
		return r.parser.defined.includes(mu(r.sliceSerialize(r.events[r.events.length - 1][1]).slice(1, -1))) ? t(e) : n(e);
	}
	function o(e) {
		return n(e);
	}
}
function $d(e, t, n) {
	return r;
	function r(t) {
		return e.enter("reference"), e.enter("referenceMarker"), e.consume(t), e.exit("referenceMarker"), i;
	}
	function i(r) {
		return r === 93 ? (e.enter("referenceMarker"), e.consume(r), e.exit("referenceMarker"), e.exit("reference"), t) : n(r);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/label-start-image.js
var ef = {
	name: "labelStartImage",
	resolveAll: Wd.resolveAll,
	tokenize: tf
};
function tf(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return e.enter("labelImage"), e.enter("labelImageMarker"), e.consume(t), e.exit("labelImageMarker"), a;
	}
	function a(t) {
		return t === 91 ? (e.enter("labelMarker"), e.consume(t), e.exit("labelMarker"), e.exit("labelImage"), o) : n(t);
	}
	function o(e) {
		/* c8 ignore next 3 */
		return e === 94 && "_hiddenFootnoteSupport" in r.parser.constructs ? n(e) : t(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/label-start-link.js
var nf = {
	name: "labelStartLink",
	resolveAll: Wd.resolveAll,
	tokenize: rf
};
function rf(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return e.enter("labelLink"), e.enter("labelMarker"), e.consume(t), e.exit("labelMarker"), e.exit("labelLink"), a;
	}
	function a(e) {
		/* c8 ignore next 3 */
		return e === 94 && "_hiddenFootnoteSupport" in r.parser.constructs ? n(e) : t(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/line-ending.js
var af = {
	name: "lineEnding",
	tokenize: of
};
function of(e, t) {
	return n;
	function n(n) {
		return e.enter("lineEnding"), e.consume(n), e.exit("lineEnding"), Ou(e, t, "linePrefix");
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/thematic-break.js
var sf = {
	name: "thematicBreak",
	tokenize: cf
};
function cf(e, t, n) {
	let r = 0, i;
	return a;
	function a(t) {
		return e.enter("thematicBreak"), o(t);
	}
	function o(e) {
		return i = e, s(e);
	}
	function s(a) {
		return a === i ? (e.enter("thematicBreakSequence"), c(a)) : r >= 3 && (a === null || $(a)) ? (e.exit("thematicBreak"), t(a)) : n(a);
	}
	function c(t) {
		return t === i ? (e.consume(t), r++, c) : (e.exit("thematicBreakSequence"), Cu(t) ? Ou(e, s, "whitespace")(t) : s(t));
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/list.js
var lf = {
	continuation: { tokenize: pf },
	exit: hf,
	name: "list",
	tokenize: ff
}, uf = {
	partial: !0,
	tokenize: gf
}, df = {
	partial: !0,
	tokenize: mf
};
function ff(e, t, n) {
	let r = this, i = r.events[r.events.length - 1], a = i && i[1].type === "linePrefix" ? i[2].sliceSerialize(i[1], !0).length : 0, o = 0;
	return s;
	function s(t) {
		let i = r.containerState.type || (t === 42 || t === 43 || t === 45 ? "listUnordered" : "listOrdered");
		if (i === "listUnordered" ? !r.containerState.marker || t === r.containerState.marker : yu(t)) {
			if (r.containerState.type || (r.containerState.type = i, e.enter(i, { _container: !0 })), i === "listUnordered") return e.enter("listItemPrefix"), t === 42 || t === 45 ? e.check(sf, n, l)(t) : l(t);
			if (!r.interrupt || t === 49) return e.enter("listItemPrefix"), e.enter("listItemValue"), c(t);
		}
		return n(t);
	}
	function c(t) {
		return yu(t) && ++o < 10 ? (e.consume(t), c) : (!r.interrupt || o < 2) && (r.containerState.marker ? t === r.containerState.marker : t === 41 || t === 46) ? (e.exit("listItemValue"), l(t)) : n(t);
	}
	function l(t) {
		return e.enter("listItemMarker"), e.consume(t), e.exit("listItemMarker"), r.containerState.marker = r.containerState.marker || t, e.check(Uu, r.interrupt ? n : u, e.attempt(uf, f, d));
	}
	function u(e) {
		return r.containerState.initialBlankLine = !0, a++, f(e);
	}
	function d(t) {
		return Cu(t) ? (e.enter("listItemPrefixWhitespace"), e.consume(t), e.exit("listItemPrefixWhitespace"), f) : n(t);
	}
	function f(n) {
		return r.containerState.size = a + r.sliceSerialize(e.exit("listItemPrefix"), !0).length, t(n);
	}
}
function pf(e, t, n) {
	let r = this;
	return r.containerState._closeFlow = void 0, e.check(Uu, i, a);
	function i(n) {
		return r.containerState.furtherBlankLines = r.containerState.furtherBlankLines || r.containerState.initialBlankLine, Ou(e, t, "listItemIndent", r.containerState.size + 1)(n);
	}
	function a(n) {
		return r.containerState.furtherBlankLines || !Cu(n) ? (r.containerState.furtherBlankLines = void 0, r.containerState.initialBlankLine = void 0, o(n)) : (r.containerState.furtherBlankLines = void 0, r.containerState.initialBlankLine = void 0, e.attempt(df, t, o)(n));
	}
	function o(i) {
		return r.containerState._closeFlow = !0, r.interrupt = void 0, Ou(e, e.attempt(lf, t, n), "linePrefix", r.parser.constructs.disable.null.includes("codeIndented") ? void 0 : 4)(i);
	}
}
function mf(e, t, n) {
	let r = this;
	return Ou(e, i, "listItemIndent", r.containerState.size + 1);
	function i(e) {
		let i = r.events[r.events.length - 1];
		return i && i[1].type === "listItemIndent" && i[2].sliceSerialize(i[1], !0).length === r.containerState.size ? t(e) : n(e);
	}
}
function hf(e) {
	e.exit(this.containerState.type);
}
function gf(e, t, n) {
	let r = this;
	return Ou(e, i, "listItemPrefixWhitespace", r.parser.constructs.disable.null.includes("codeIndented") ? void 0 : 5);
	function i(e) {
		let i = r.events[r.events.length - 1];
		return !Cu(e) && i && i[1].type === "listItemPrefixWhitespace" ? t(e) : n(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-core-commonmark/lib/setext-underline.js
var _f = {
	name: "setextUnderline",
	resolveTo: vf,
	tokenize: yf
};
function vf(e, t) {
	let n = e.length, r, i, a;
	for (; n--;) if (e[n][0] === "enter") {
		if (e[n][1].type === "content") {
			r = n;
			break;
		}
		e[n][1].type === "paragraph" && (i = n);
	} else e[n][1].type === "content" && e.splice(n, 1), !a && e[n][1].type === "definition" && (a = n);
	let o = {
		type: "setextHeading",
		start: { ...e[r][1].start },
		end: { ...e[e.length - 1][1].end }
	};
	return e[i][1].type = "setextHeadingText", a ? (e.splice(i, 0, [
		"enter",
		o,
		t
	]), e.splice(a + 1, 0, [
		"exit",
		e[r][1],
		t
	]), e[r][1].end = { ...e[a][1].end }) : e[r][1] = o, e.push([
		"exit",
		o,
		t
	]), e;
}
function yf(e, t, n) {
	let r = this, i;
	return a;
	function a(t) {
		let a = r.events.length, s;
		for (; a--;) if (r.events[a][1].type !== "lineEnding" && r.events[a][1].type !== "linePrefix" && r.events[a][1].type !== "content") {
			s = r.events[a][1].type === "paragraph";
			break;
		}
		return !r.parser.lazy[r.now().line] && (r.interrupt || s) ? (e.enter("setextHeadingLine"), i = t, o(t)) : n(t);
	}
	function o(t) {
		return e.enter("setextHeadingLineSequence"), s(t);
	}
	function s(t) {
		return t === i ? (e.consume(t), s) : (e.exit("setextHeadingLineSequence"), Cu(t) ? Ou(e, c, "lineSuffix")(t) : c(t));
	}
	function c(r) {
		return r === null || $(r) ? (e.exit("setextHeadingLine"), t(r)) : n(r);
	}
}
//#endregion
//#region frontend/node_modules/micromark/lib/initialize/flow.js
var bf = { tokenize: xf };
function xf(e) {
	let t = this, n = e.attempt(Uu, r, e.attempt(this.parser.constructs.flowInitial, i, Ou(e, e.attempt(this.parser.constructs.flow, i, e.attempt(hd, i)), "linePrefix")));
	return n;
	function r(r) {
		if (r === null) {
			e.consume(r);
			return;
		}
		return e.enter("lineEndingBlank"), e.consume(r), e.exit("lineEndingBlank"), t.currentConstruct = void 0, n;
	}
	function i(r) {
		if (r === null) {
			e.consume(r);
			return;
		}
		return e.enter("lineEnding"), e.consume(r), e.exit("lineEnding"), t.currentConstruct = void 0, n;
	}
}
//#endregion
//#region frontend/node_modules/micromark/lib/initialize/text.js
var Sf = { resolveAll: Ef() }, Cf = Tf("string"), wf = Tf("text");
function Tf(e) {
	return {
		resolveAll: Ef(e === "text" ? Df : void 0),
		tokenize: t
	};
	function t(t) {
		let n = this, r = this.parser.constructs[e], i = t.attempt(r, a, o);
		return a;
		function a(e) {
			return c(e) ? i(e) : o(e);
		}
		function o(e) {
			if (e === null) {
				t.consume(e);
				return;
			}
			return t.enter("data"), t.consume(e), s;
		}
		function s(e) {
			return c(e) ? (t.exit("data"), i(e)) : (t.consume(e), s);
		}
		function c(e) {
			if (e === null) return !0;
			let t = r[e], i = -1;
			if (t) for (; ++i < t.length;) {
				let e = t[i];
				if (!e.previous || e.previous.call(n, n.previous)) return !0;
			}
			return !1;
		}
	}
}
function Ef(e) {
	return t;
	function t(t, n) {
		let r = -1, i;
		for (; ++r <= t.length;) i === void 0 ? t[r] && t[r][1].type === "data" && (i = r, r++) : (!t[r] || t[r][1].type !== "data") && (r !== i + 2 && (t[i][1].end = t[r - 1][1].end, t.splice(i + 2, r - i - 2), r = i + 2), i = void 0);
		return e ? e(t, n) : t;
	}
}
function Df(e, t) {
	let n = 0;
	for (; ++n <= e.length;) if ((n === e.length || e[n][1].type === "lineEnding") && e[n - 1][1].type === "data") {
		let r = e[n - 1][1], i = t.sliceStream(r), a = i.length, o = -1, s = 0, c;
		for (; a--;) {
			let e = i[a];
			if (typeof e == "string") {
				for (o = e.length; e.charCodeAt(o - 1) === 32;) s++, o--;
				if (o) break;
				o = -1;
			} else if (e === -2) c = !0, s++;
			else if (e !== -1) {
				a++;
				break;
			}
		}
		if (t._contentTypeTextTrailing && n === e.length && (s = 0), s) {
			let i = {
				type: n === e.length || c || s < 2 ? "lineSuffix" : "hardBreakTrailing",
				start: {
					_bufferIndex: a ? o : r.start._bufferIndex + o,
					_index: r.start._index + a,
					line: r.end.line,
					column: r.end.column - s,
					offset: r.end.offset - s
				},
				end: { ...r.end }
			};
			r.end = { ...i.start }, r.start.offset === r.end.offset ? Object.assign(r, i) : (e.splice(n, 0, [
				"enter",
				i,
				t
			], [
				"exit",
				i,
				t
			]), n += 2);
		}
		n++;
	}
	return e;
}
//#endregion
//#region frontend/node_modules/micromark/lib/constructs.js
var Of = /* @__PURE__ */ s({
	attentionMarkers: () => If,
	contentInitial: () => Af,
	disable: () => Lf,
	document: () => kf,
	flow: () => Mf,
	flowInitial: () => jf,
	insideSpan: () => Ff,
	string: () => Nf,
	text: () => Pf
}), kf = {
	42: lf,
	43: lf,
	45: lf,
	48: lf,
	49: lf,
	50: lf,
	51: lf,
	52: lf,
	53: lf,
	54: lf,
	55: lf,
	56: lf,
	57: lf,
	62: Gu
}, Af = { 91: wd }, jf = {
	[-2]: rd,
	[-1]: rd,
	32: rd
}, Mf = {
	35: Ad,
	42: sf,
	45: [_f, sf],
	60: Fd,
	61: _f,
	95: sf,
	96: ed,
	126: ed
}, Nf = {
	38: Zu,
	92: Yu
}, Pf = {
	[-5]: af,
	[-4]: af,
	[-3]: af,
	33: ef,
	38: Zu,
	42: Lu,
	60: [Vu, Hd],
	91: nf,
	92: [Od, Yu],
	93: Wd,
	95: Lu,
	96: sd
}, Ff = { null: [Lu, Sf] }, If = { null: [42, 95] }, Lf = { null: [] };
//#endregion
//#region frontend/node_modules/micromark/lib/create-tokenizer.js
function Rf(e, t, n) {
	let r = {
		_bufferIndex: -1,
		_index: 0,
		line: n && n.line || 1,
		column: n && n.column || 1,
		offset: n && n.offset || 0
	}, i = {}, a = [], o = [], s = [], c = {
		attempt: C(x),
		check: C(S),
		consume: v,
		enter: y,
		exit: b,
		interrupt: C(S, { interrupt: !0 })
	}, l = {
		code: null,
		containerState: {},
		defineSkip: h,
		events: [],
		now: m,
		parser: e,
		previous: null,
		sliceSerialize: f,
		sliceStream: p,
		write: d
	}, u = t.tokenize.call(l, c);
	return t.resolveAll && a.push(t), l;
	function d(e) {
		return o = cu(o, e), g(), o[o.length - 1] === null ? (w(t, 0), l.events = Iu(a, l.events, l), l.events) : [];
	}
	function f(e, t) {
		return Bf(p(e), t);
	}
	function p(e) {
		return zf(o, e);
	}
	function m() {
		let { _bufferIndex: e, _index: t, line: n, column: i, offset: a } = r;
		return {
			_bufferIndex: e,
			_index: t,
			line: n,
			column: i,
			offset: a
		};
	}
	function h(e) {
		i[e.line] = e.column, E();
	}
	function g() {
		let e;
		for (; r._index < o.length;) {
			let t = o[r._index];
			if (typeof t == "string") for (e = r._index, r._bufferIndex < 0 && (r._bufferIndex = 0); r._index === e && r._bufferIndex < t.length;) _(t.charCodeAt(r._bufferIndex));
			else _(t);
		}
	}
	function _(e) {
		u = u(e);
	}
	function v(e) {
		$(e) ? (r.line++, r.column = 1, r.offset += e === -3 ? 2 : 1, E()) : e !== -1 && (r.column++, r.offset++), r._bufferIndex < 0 ? r._index++ : (r._bufferIndex++, r._bufferIndex === o[r._index].length && (r._bufferIndex = -1, r._index++)), l.previous = e;
	}
	function y(e, t) {
		let n = t || {};
		return n.type = e, n.start = m(), l.events.push([
			"enter",
			n,
			l
		]), s.push(n), n;
	}
	function b(e) {
		let t = s.pop();
		return t.end = m(), l.events.push([
			"exit",
			t,
			l
		]), t;
	}
	function x(e, t) {
		w(e, t.from);
	}
	function S(e, t) {
		t.restore();
	}
	function C(e, t) {
		return n;
		function n(n, r, i) {
			let a, o, s, u;
			return Array.isArray(n) ? f(n) : "tokenize" in n ? f([n]) : d(n);
			function d(e) {
				return t;
				function t(t) {
					let n = t !== null && e[t], r = t !== null && e.null;
					return f([...Array.isArray(n) ? n : n ? [n] : [], ...Array.isArray(r) ? r : r ? [r] : []])(t);
				}
			}
			function f(e) {
				return a = e, o = 0, e.length === 0 ? i : p(e[o]);
			}
			function p(e) {
				return n;
				function n(n) {
					return u = T(), s = e, e.partial || (l.currentConstruct = e), e.name && l.parser.constructs.disable.null.includes(e.name) ? h(n) : e.tokenize.call(t ? Object.assign(Object.create(l), t) : l, c, m, h)(n);
				}
			}
			function m(t) {
				return e(s, u), r;
			}
			function h(e) {
				return u.restore(), ++o < a.length ? p(a[o]) : i;
			}
		}
	}
	function w(e, t) {
		e.resolveAll && !a.includes(e) && a.push(e), e.resolve && su(l.events, t, l.events.length - t, e.resolve(l.events.slice(t), l)), e.resolveTo && (l.events = e.resolveTo(l.events, l));
	}
	function T() {
		let e = m(), t = l.previous, n = l.currentConstruct, i = l.events.length, a = Array.from(s);
		return {
			from: i,
			restore: o
		};
		function o() {
			r = e, l.previous = t, l.currentConstruct = n, l.events.length = i, s = a, E();
		}
	}
	function E() {
		r.line in i && r.column < 2 && (r.column = i[r.line], r.offset += i[r.line] - 1);
	}
}
function zf(e, t) {
	let n = t.start._index, r = t.start._bufferIndex, i = t.end._index, a = t.end._bufferIndex, o;
	if (n === i) o = [e[n].slice(r, a)];
	else {
		if (o = e.slice(n, i), r > -1) {
			let e = o[0];
			typeof e == "string" ? o[0] = e.slice(r) : o.shift();
		}
		a > 0 && o.push(e[i].slice(0, a));
	}
	return o;
}
function Bf(e, t) {
	let n = -1, r = [], i;
	for (; ++n < e.length;) {
		let a = e[n], o;
		if (typeof a == "string") o = a;
		else switch (a) {
			case -5:
				o = "\r";
				break;
			case -4:
				o = "\n";
				break;
			case -3:
				o = "\r\n";
				break;
			case -2:
				o = t ? " " : "	";
				break;
			case -1:
				if (!t && i) continue;
				o = " ";
				break;
			default: o = String.fromCharCode(a);
		}
		i = a === -2, r.push(o);
	}
	return r.join("");
}
//#endregion
//#region frontend/node_modules/micromark/lib/parse.js
function Vf(e) {
	let t = {
		constructs: uu([Of, ...(e || {}).extensions || []]),
		content: n(ku),
		defined: [],
		document: n(ju),
		flow: n(bf),
		lazy: {},
		string: n(Cf),
		text: n(wf)
	};
	return t;
	function n(e) {
		return n;
		function n(n) {
			return Rf(t, e, n);
		}
	}
}
//#endregion
//#region frontend/node_modules/micromark/lib/postprocess.js
function Hf(e) {
	for (; !pd(e););
	return e;
}
//#endregion
//#region frontend/node_modules/micromark/lib/preprocess.js
var Uf = /[\0\t\n\r]/g;
function Wf() {
	let e = 1, t = "", n = !0, r;
	return i;
	function i(i, a, o) {
		let s = [], c, l, u, d, f;
		for (i = t + (typeof i == "string" ? i.toString() : new TextDecoder(a || void 0).decode(i)), u = 0, t = "", n &&= (i.charCodeAt(0) === 65279 && u++, void 0); u < i.length;) {
			if (Uf.lastIndex = u, c = Uf.exec(i), d = c && c.index !== void 0 ? c.index : i.length, f = i.charCodeAt(d), !c) {
				t = i.slice(u);
				break;
			}
			if (f === 10 && u === d && r) s.push(-3), r = void 0;
			else switch (r &&= (s.push(-5), void 0), u < d && (s.push(i.slice(u, d)), e += d - u), f) {
				case 0:
					s.push(65533), e++;
					break;
				case 9:
					for (l = Math.ceil(e / 4) * 4, s.push(-2); e++ < l;) s.push(-1);
					break;
				case 10:
					s.push(-4), e = 1;
					break;
				default: r = !0, e = 1;
			}
			u = d + 1;
		}
		return o && (r && s.push(-5), t && s.push(t), s.push(null)), s;
	}
}
//#endregion
//#region frontend/node_modules/micromark-util-decode-string/index.js
var Gf = /\\([!-/:-@[-`{-~])|&(#(?:\d{1,7}|x[\da-f]{1,6})|[\da-z]{1,31});/gi;
function Kf(e) {
	return e.replace(Gf, qf);
}
function qf(e, t, n) {
	if (t) return t;
	if (n.charCodeAt(0) === 35) {
		let e = n.charCodeAt(1), t = e === 120 || e === 88;
		return pu(n.slice(t ? 2 : 1), t ? 16 : 10);
	}
	return ou(n) || e;
}
//#endregion
//#region frontend/node_modules/mdast-util-from-markdown/lib/index.js
var Jf = {}.hasOwnProperty;
function Yf(e, t, n) {
	return t && typeof t == "object" && (n = t, t = void 0), Xf(n)(Hf(Vf(n).document().write(Wf()(e, t, !0))));
}
function Xf(e) {
	let t = {
		transforms: [],
		canContainEols: [
			"emphasis",
			"fragment",
			"heading",
			"paragraph",
			"strong"
		],
		enter: {
			autolink: a(Te),
			autolinkProtocol: T,
			autolinkEmail: T,
			atxHeading: a(xe),
			blockQuote: a(ge),
			characterEscape: T,
			characterReference: T,
			codeFenced: a(_e),
			codeFencedFenceInfo: o,
			codeFencedFenceMeta: o,
			codeIndented: a(_e, o),
			codeText: a(ve, o),
			codeTextData: T,
			data: T,
			codeFlowValue: T,
			definition: a(ye),
			definitionDestinationString: o,
			definitionLabelString: o,
			definitionTitleString: o,
			emphasis: a(be),
			hardBreakEscape: a(Se),
			hardBreakTrailing: a(Se),
			htmlFlow: a(Ce, o),
			htmlFlowData: T,
			htmlText: a(Ce, o),
			htmlTextData: T,
			image: a(we),
			label: o,
			link: a(Te),
			listItem: a(De),
			listItemValue: f,
			listOrdered: a(Ee, d),
			listUnordered: a(Ee),
			paragraph: a(Oe),
			reference: le,
			referenceString: o,
			resourceDestinationString: o,
			resourceTitleString: o,
			setextHeading: a(xe),
			strong: a(ke),
			thematicBreak: a(je)
		},
		exit: {
			atxHeading: c(),
			atxHeadingSequence: x,
			autolink: c(),
			autolinkEmail: he,
			autolinkProtocol: me,
			blockQuote: c(),
			characterEscapeValue: E,
			characterReferenceMarkerHexadecimal: de,
			characterReferenceMarkerNumeric: de,
			characterReferenceValue: fe,
			characterReference: pe,
			codeFenced: c(g),
			codeFencedFence: h,
			codeFencedFenceInfo: p,
			codeFencedFenceMeta: m,
			codeFlowValue: E,
			codeIndented: c(_),
			codeText: c(re),
			codeTextData: E,
			data: E,
			definition: c(),
			definitionDestinationString: b,
			definitionLabelString: v,
			definitionTitleString: y,
			emphasis: c(),
			hardBreakEscape: c(D),
			hardBreakTrailing: c(D),
			htmlFlow: c(te),
			htmlFlowData: E,
			htmlText: c(ne),
			htmlTextData: E,
			image: c(ae),
			label: se,
			labelText: oe,
			lineEnding: ee,
			link: c(ie),
			listItem: c(),
			listOrdered: c(),
			listUnordered: c(),
			paragraph: c(),
			referenceString: ue,
			resourceDestinationString: O,
			resourceTitleString: k,
			resource: ce,
			setextHeading: c(w),
			setextHeadingLineSequence: C,
			setextHeadingText: S,
			strong: c(),
			thematicBreak: c()
		}
	};
	Qf(t, (e || {}).mdastExtensions || []);
	let n = {};
	return r;
	function r(e) {
		let r = {
			type: "root",
			children: []
		}, a = {
			stack: [r],
			tokenStack: [],
			config: t,
			enter: s,
			exit: l,
			buffer: o,
			resume: u,
			data: n
		}, c = [], d = -1;
		for (; ++d < e.length;) (e[d][1].type === "listOrdered" || e[d][1].type === "listUnordered") && (e[d][0] === "enter" ? c.push(d) : d = i(e, c.pop(), d));
		for (d = -1; ++d < e.length;) {
			let n = t[e[d][0]];
			Jf.call(n, e[d][1].type) && n[e[d][1].type].call(Object.assign({ sliceSerialize: e[d][2].sliceSerialize }, a), e[d][1]);
		}
		if (a.tokenStack.length > 0) {
			let e = a.tokenStack[a.tokenStack.length - 1];
			(e[1] || ep).call(a, void 0, e[0]);
		}
		for (r.position = {
			start: Zf(e.length > 0 ? e[0][1].start : {
				line: 1,
				column: 1,
				offset: 0
			}),
			end: Zf(e.length > 0 ? e[e.length - 2][1].end : {
				line: 1,
				column: 1,
				offset: 0
			})
		}, d = -1; ++d < t.transforms.length;) r = t.transforms[d](r) || r;
		return r;
	}
	function i(e, t, n) {
		let r = t - 1, i = -1, a = !1, o, s, c, l;
		for (; ++r <= n;) {
			let t = e[r];
			switch (t[1].type) {
				case "listUnordered":
				case "listOrdered":
				case "blockQuote":
					t[0] === "enter" ? i++ : i--, l = void 0;
					break;
				case "lineEndingBlank":
					t[0] === "enter" && (o && !l && !i && !c && (c = r), l = void 0);
					break;
				case "linePrefix":
				case "listItemValue":
				case "listItemMarker":
				case "listItemPrefix":
				case "listItemPrefixWhitespace": break;
				default: l = void 0;
			}
			if (!i && t[0] === "enter" && t[1].type === "listItemPrefix" || i === -1 && t[0] === "exit" && (t[1].type === "listUnordered" || t[1].type === "listOrdered")) {
				if (o) {
					let i = r;
					for (s = void 0; i--;) {
						let t = e[i];
						if (t[1].type === "lineEnding" || t[1].type === "lineEndingBlank") {
							if (t[0] === "exit") continue;
							s && (e[s][1].type = "lineEndingBlank", a = !0), t[1].type = "lineEnding", s = i;
						} else if (!(t[1].type === "linePrefix" || t[1].type === "blockQuotePrefix" || t[1].type === "blockQuotePrefixWhitespace" || t[1].type === "blockQuoteMarker" || t[1].type === "listItemIndent")) break;
					}
					c && (!s || c < s) && (o._spread = !0), o.end = Object.assign({}, s ? e[s][1].start : t[1].end), e.splice(s || r, 0, [
						"exit",
						o,
						t[2]
					]), r++, n++;
				}
				if (t[1].type === "listItemPrefix") {
					let i = {
						type: "listItem",
						_spread: !1,
						start: Object.assign({}, t[1].start),
						end: void 0
					};
					o = i, e.splice(r, 0, [
						"enter",
						i,
						t[2]
					]), r++, n++, c = void 0, l = !0;
				}
			}
		}
		return e[t][1]._spread = a, n;
	}
	function a(e, t) {
		return n;
		function n(n) {
			s.call(this, e(n), n), t && t.call(this, n);
		}
	}
	function o() {
		this.stack.push({
			type: "fragment",
			children: []
		});
	}
	function s(e, t, n) {
		this.stack[this.stack.length - 1].children.push(e), this.stack.push(e), this.tokenStack.push([t, n || void 0]), e.position = {
			start: Zf(t.start),
			end: void 0
		};
	}
	function c(e) {
		return t;
		function t(t) {
			e && e.call(this, t), l.call(this, t);
		}
	}
	function l(e, t) {
		let n = this.stack.pop(), r = this.tokenStack.pop();
		if (r) r[0].type !== e.type && (t ? t.call(this, e, r[0]) : (r[1] || ep).call(this, e, r[0]));
		else throw Error("Cannot close `" + e.type + "` (" + yl({
			start: e.start,
			end: e.end
		}) + "): it’s not open");
		n.position.end = Zf(e.end);
	}
	function u() {
		return tu(this.stack.pop());
	}
	function d() {
		this.data.expectingFirstListItemValue = !0;
	}
	function f(e) {
		if (this.data.expectingFirstListItemValue) {
			let t = this.stack[this.stack.length - 2];
			t.start = Number.parseInt(this.sliceSerialize(e), 10), this.data.expectingFirstListItemValue = void 0;
		}
	}
	function p() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.lang = e;
	}
	function m() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.meta = e;
	}
	function h() {
		this.data.flowCodeInside || (this.buffer(), this.data.flowCodeInside = !0);
	}
	function g() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.value = e.replace(/^(\r?\n|\r)|(\r?\n|\r)$/g, ""), this.data.flowCodeInside = void 0;
	}
	function _() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.value = e.replace(/(\r?\n|\r)$/g, "");
	}
	function v(e) {
		let t = this.resume(), n = this.stack[this.stack.length - 1];
		n.label = t, n.identifier = mu(this.sliceSerialize(e)).toLowerCase();
	}
	function y() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.title = e;
	}
	function b() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.url = e;
	}
	function x(e) {
		let t = this.stack[this.stack.length - 1];
		t.depth ||= this.sliceSerialize(e).length;
	}
	function S() {
		this.data.setextHeadingSlurpLineEnding = !0;
	}
	function C(e) {
		let t = this.stack[this.stack.length - 1];
		t.depth = this.sliceSerialize(e).codePointAt(0) === 61 ? 1 : 2;
	}
	function w() {
		this.data.setextHeadingSlurpLineEnding = void 0;
	}
	function T(e) {
		let t = this.stack[this.stack.length - 1].children, n = t[t.length - 1];
		(!n || n.type !== "text") && (n = Ae(), n.position = {
			start: Zf(e.start),
			end: void 0
		}, t.push(n)), this.stack.push(n);
	}
	function E(e) {
		let t = this.stack.pop();
		t.value += this.sliceSerialize(e), t.position.end = Zf(e.end);
	}
	function ee(e) {
		let n = this.stack[this.stack.length - 1];
		if (this.data.atHardBreak) {
			let t = n.children[n.children.length - 1];
			t.position.end = Zf(e.end), this.data.atHardBreak = void 0;
			return;
		}
		!this.data.setextHeadingSlurpLineEnding && t.canContainEols.includes(n.type) && (T.call(this, e), E.call(this, e));
	}
	function D() {
		this.data.atHardBreak = !0;
	}
	function te() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.value = e;
	}
	function ne() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.value = e;
	}
	function re() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.value = e;
	}
	function ie() {
		let e = this.stack[this.stack.length - 1];
		if (this.data.inReference) {
			let t = this.data.referenceType || "shortcut";
			e.type += "Reference", e.referenceType = t, delete e.url, delete e.title;
		} else delete e.identifier, delete e.label;
		this.data.referenceType = void 0;
	}
	function ae() {
		let e = this.stack[this.stack.length - 1];
		if (this.data.inReference) {
			let t = this.data.referenceType || "shortcut";
			e.type += "Reference", e.referenceType = t, delete e.url, delete e.title;
		} else delete e.identifier, delete e.label;
		this.data.referenceType = void 0;
	}
	function oe(e) {
		let t = this.sliceSerialize(e), n = this.stack[this.stack.length - 2];
		n.label = Kf(t), n.identifier = mu(t).toLowerCase();
	}
	function se() {
		let e = this.stack[this.stack.length - 1], t = this.resume(), n = this.stack[this.stack.length - 1];
		this.data.inReference = !0, n.type === "link" ? n.children = e.children : n.alt = t;
	}
	function O() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.url = e;
	}
	function k() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.title = e;
	}
	function ce() {
		this.data.inReference = void 0;
	}
	function le() {
		this.data.referenceType = "collapsed";
	}
	function ue(e) {
		let t = this.resume(), n = this.stack[this.stack.length - 1];
		n.label = t, n.identifier = mu(this.sliceSerialize(e)).toLowerCase(), this.data.referenceType = "full";
	}
	function de(e) {
		this.data.characterReferenceType = e.type;
	}
	function fe(e) {
		let t = this.sliceSerialize(e), n = this.data.characterReferenceType, r;
		n ? (r = pu(t, n === "characterReferenceMarkerNumeric" ? 10 : 16), this.data.characterReferenceType = void 0) : r = ou(t);
		let i = this.stack[this.stack.length - 1];
		i.value += r;
	}
	function pe(e) {
		let t = this.stack.pop();
		t.position.end = Zf(e.end);
	}
	function me(e) {
		E.call(this, e);
		let t = this.stack[this.stack.length - 1];
		t.url = this.sliceSerialize(e);
	}
	function he(e) {
		E.call(this, e);
		let t = this.stack[this.stack.length - 1];
		t.url = "mailto:" + this.sliceSerialize(e);
	}
	function ge() {
		return {
			type: "blockquote",
			children: []
		};
	}
	function _e() {
		return {
			type: "code",
			lang: null,
			meta: null,
			value: ""
		};
	}
	function ve() {
		return {
			type: "inlineCode",
			value: ""
		};
	}
	function ye() {
		return {
			type: "definition",
			identifier: "",
			label: null,
			title: null,
			url: ""
		};
	}
	function be() {
		return {
			type: "emphasis",
			children: []
		};
	}
	function xe() {
		return {
			type: "heading",
			depth: 0,
			children: []
		};
	}
	function Se() {
		return { type: "break" };
	}
	function Ce() {
		return {
			type: "html",
			value: ""
		};
	}
	function we() {
		return {
			type: "image",
			title: null,
			url: "",
			alt: null
		};
	}
	function Te() {
		return {
			type: "link",
			title: null,
			url: "",
			children: []
		};
	}
	function Ee(e) {
		return {
			type: "list",
			ordered: e.type === "listOrdered",
			start: null,
			spread: e._spread,
			children: []
		};
	}
	function De(e) {
		return {
			type: "listItem",
			spread: e._spread,
			checked: null,
			children: []
		};
	}
	function Oe() {
		return {
			type: "paragraph",
			children: []
		};
	}
	function ke() {
		return {
			type: "strong",
			children: []
		};
	}
	function Ae() {
		return {
			type: "text",
			value: ""
		};
	}
	function je() {
		return { type: "thematicBreak" };
	}
}
function Zf(e) {
	return {
		line: e.line,
		column: e.column,
		offset: e.offset
	};
}
function Qf(e, t) {
	let n = -1;
	for (; ++n < t.length;) {
		let r = t[n];
		Array.isArray(r) ? Qf(e, r) : $f(e, r);
	}
}
function $f(e, t) {
	let n;
	for (n in t) if (Jf.call(t, n)) switch (n) {
		case "canContainEols": {
			let r = t[n];
			r && e[n].push(...r);
			break;
		}
		case "transforms": {
			let r = t[n];
			r && e[n].push(...r);
			break;
		}
		case "enter":
		case "exit": {
			let r = t[n];
			r && Object.assign(e[n], r);
			break;
		}
	}
}
function ep(e, t) {
	throw Error(e ? "Cannot close `" + e.type + "` (" + yl({
		start: e.start,
		end: e.end
	}) + "): a different token (`" + t.type + "`, " + yl({
		start: t.start,
		end: t.end
	}) + ") is open" : "Cannot close document, a token (`" + t.type + "`, " + yl({
		start: t.start,
		end: t.end
	}) + ") is still open");
}
//#endregion
//#region frontend/node_modules/remark-parse/lib/index.js
function tp(e) {
	let t = this;
	t.parser = n;
	function n(n) {
		return Yf(n, {
			...t.data("settings"),
			...e,
			extensions: t.data("micromarkExtensions") || [],
			mdastExtensions: t.data("fromMarkdownExtensions") || []
		});
	}
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/blockquote.js
function np(e, t) {
	let n = {
		type: "element",
		tagName: "blockquote",
		properties: {},
		children: e.wrap(e.all(t), !0)
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/break.js
function rp(e, t) {
	let n = {
		type: "element",
		tagName: "br",
		properties: {},
		children: []
	};
	return e.patch(t, n), [e.applyData(t, n), {
		type: "text",
		value: "\n"
	}];
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/code.js
function ip(e, t) {
	let n = t.value ? t.value + "\n" : "", r = {}, i = t.lang ? t.lang.split(/\s+/) : [];
	i.length > 0 && (r.className = ["language-" + i[0]]);
	let a = {
		type: "element",
		tagName: "code",
		properties: r,
		children: [{
			type: "text",
			value: n
		}]
	};
	return t.meta && (a.data = { meta: t.meta }), e.patch(t, a), a = e.applyData(t, a), a = {
		type: "element",
		tagName: "pre",
		properties: {},
		children: [a]
	}, e.patch(t, a), a;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/delete.js
function ap(e, t) {
	let n = {
		type: "element",
		tagName: "del",
		properties: {},
		children: e.all(t)
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/emphasis.js
function op(e, t) {
	let n = {
		type: "element",
		tagName: "em",
		properties: {},
		children: e.all(t)
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/footnote-reference.js
function sp(e, t) {
	let n = typeof e.options.clobberPrefix == "string" ? e.options.clobberPrefix : "user-content-", r = String(t.identifier).toUpperCase(), i = Du(r.toLowerCase()), a = e.footnoteOrder.indexOf(r), o, s = e.footnoteCounts.get(r);
	s === void 0 ? (s = 0, e.footnoteOrder.push(r), o = e.footnoteOrder.length) : o = a + 1, s += 1, e.footnoteCounts.set(r, s);
	let c = {
		type: "element",
		tagName: "a",
		properties: {
			href: "#" + n + "fn-" + i,
			id: n + "fnref-" + i + (s > 1 ? "-" + s : ""),
			dataFootnoteRef: !0,
			ariaDescribedBy: ["footnote-label"]
		},
		children: [{
			type: "text",
			value: String(o)
		}]
	};
	e.patch(t, c);
	let l = {
		type: "element",
		tagName: "sup",
		properties: {},
		children: [c]
	};
	return e.patch(t, l), e.applyData(t, l);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/heading.js
function cp(e, t) {
	let n = {
		type: "element",
		tagName: "h" + t.depth,
		properties: {},
		children: e.all(t)
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/html.js
function lp(e, t) {
	if (e.options.allowDangerousHtml) {
		let n = {
			type: "raw",
			value: t.value
		};
		return e.patch(t, n), e.applyData(t, n);
	}
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/revert.js
function up(e, t) {
	let n = t.referenceType, r = "]";
	if (n === "collapsed" ? r += "[]" : n === "full" && (r += "[" + (t.label || t.identifier) + "]"), t.type === "imageReference") return [{
		type: "text",
		value: "![" + t.alt + r
	}];
	let i = e.all(t), a = i[0];
	a && a.type === "text" ? a.value = "[" + a.value : i.unshift({
		type: "text",
		value: "["
	});
	let o = i[i.length - 1];
	return o && o.type === "text" ? o.value += r : i.push({
		type: "text",
		value: r
	}), i;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/image-reference.js
function dp(e, t) {
	let n = String(t.identifier).toUpperCase(), r = e.definitionById.get(n);
	if (!r) return up(e, t);
	let i = {
		src: Du(r.url || ""),
		alt: t.alt
	};
	r.title !== null && r.title !== void 0 && (i.title = r.title);
	let a = {
		type: "element",
		tagName: "img",
		properties: i,
		children: []
	};
	return e.patch(t, a), e.applyData(t, a);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/image.js
function fp(e, t) {
	let n = { src: Du(t.url) };
	t.alt !== null && t.alt !== void 0 && (n.alt = t.alt), t.title !== null && t.title !== void 0 && (n.title = t.title);
	let r = {
		type: "element",
		tagName: "img",
		properties: n,
		children: []
	};
	return e.patch(t, r), e.applyData(t, r);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/inline-code.js
function pp(e, t) {
	let n = {
		type: "text",
		value: t.value.replace(/\r?\n|\r/g, " ")
	};
	e.patch(t, n);
	let r = {
		type: "element",
		tagName: "code",
		properties: {},
		children: [n]
	};
	return e.patch(t, r), e.applyData(t, r);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/link-reference.js
function mp(e, t) {
	let n = String(t.identifier).toUpperCase(), r = e.definitionById.get(n);
	if (!r) return up(e, t);
	let i = { href: Du(r.url || "") };
	r.title !== null && r.title !== void 0 && (i.title = r.title);
	let a = {
		type: "element",
		tagName: "a",
		properties: i,
		children: e.all(t)
	};
	return e.patch(t, a), e.applyData(t, a);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/link.js
function hp(e, t) {
	let n = { href: Du(t.url) };
	t.title !== null && t.title !== void 0 && (n.title = t.title);
	let r = {
		type: "element",
		tagName: "a",
		properties: n,
		children: e.all(t)
	};
	return e.patch(t, r), e.applyData(t, r);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/list-item.js
function gp(e, t, n) {
	let r = e.all(t), i = n ? _p(n) : vp(t), a = {}, o = [];
	if (typeof t.checked == "boolean") {
		let e = r[0], n;
		e && e.type === "element" && e.tagName === "p" ? n = e : (n = {
			type: "element",
			tagName: "p",
			properties: {},
			children: []
		}, r.unshift(n)), n.children.length > 0 && n.children.unshift({
			type: "text",
			value: " "
		}), n.children.unshift({
			type: "element",
			tagName: "input",
			properties: {
				type: "checkbox",
				checked: t.checked,
				disabled: !0
			},
			children: []
		}), a.className = ["task-list-item"];
	}
	let s = -1;
	for (; ++s < r.length;) {
		let e = r[s];
		(i || s !== 0 || e.type !== "element" || e.tagName !== "p") && o.push({
			type: "text",
			value: "\n"
		}), e.type === "element" && e.tagName === "p" && !i ? o.push(...e.children) : o.push(e);
	}
	let c = r[r.length - 1];
	c && (i || c.type !== "element" || c.tagName !== "p") && o.push({
		type: "text",
		value: "\n"
	});
	let l = {
		type: "element",
		tagName: "li",
		properties: a,
		children: o
	};
	return e.patch(t, l), e.applyData(t, l);
}
function _p(e) {
	let t = !1;
	if (e.type === "list") {
		t = e.spread || !1;
		let n = e.children, r = -1;
		for (; !t && ++r < n.length;) t = vp(n[r]);
	}
	return t;
}
function vp(e) {
	return e.spread ?? e.children.length > 1;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/list.js
function yp(e, t) {
	let n = {}, r = e.all(t), i = -1;
	for (typeof t.start == "number" && t.start !== 1 && (n.start = t.start); ++i < r.length;) {
		let e = r[i];
		if (e.type === "element" && e.tagName === "li" && e.properties && Array.isArray(e.properties.className) && e.properties.className.includes("task-list-item")) {
			n.className = ["contains-task-list"];
			break;
		}
	}
	let a = {
		type: "element",
		tagName: t.ordered ? "ol" : "ul",
		properties: n,
		children: e.wrap(r, !0)
	};
	return e.patch(t, a), e.applyData(t, a);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/paragraph.js
function bp(e, t) {
	let n = {
		type: "element",
		tagName: "p",
		properties: {},
		children: e.all(t)
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/root.js
function xp(e, t) {
	let n = {
		type: "root",
		children: e.wrap(e.all(t))
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/strong.js
function Sp(e, t) {
	let n = {
		type: "element",
		tagName: "strong",
		properties: {},
		children: e.all(t)
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/table.js
function Cp(e, t) {
	let n = e.all(t), r = n.shift(), i = [];
	if (r) {
		let n = {
			type: "element",
			tagName: "thead",
			properties: {},
			children: e.wrap([r], !0)
		};
		e.patch(t.children[0], n), i.push(n);
	}
	if (n.length > 0) {
		let r = {
			type: "element",
			tagName: "tbody",
			properties: {},
			children: e.wrap(n, !0)
		}, a = gl(t.children[1]), o = hl(t.children[t.children.length - 1]);
		a && o && (r.position = {
			start: a,
			end: o
		}), i.push(r);
	}
	let a = {
		type: "element",
		tagName: "table",
		properties: {},
		children: e.wrap(i, !0)
	};
	return e.patch(t, a), e.applyData(t, a);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/table-row.js
function wp(e, t, n) {
	let r = n ? n.children : void 0, i = (r ? r.indexOf(t) : 1) === 0 ? "th" : "td", a = n && n.type === "table" ? n.align : void 0, o = a ? a.length : t.children.length, s = -1, c = [];
	for (; ++s < o;) {
		let n = t.children[s], r = {}, o = a ? a[s] : void 0;
		o && (r.align = o);
		let l = {
			type: "element",
			tagName: i,
			properties: r,
			children: []
		};
		n && (l.children = e.all(n), e.patch(n, l), l = e.applyData(n, l)), c.push(l);
	}
	let l = {
		type: "element",
		tagName: "tr",
		properties: {},
		children: e.wrap(c, !0)
	};
	return e.patch(t, l), e.applyData(t, l);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/table-cell.js
function Tp(e, t) {
	let n = {
		type: "element",
		tagName: "td",
		properties: {},
		children: e.all(t)
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/trim-lines/index.js
var Ep = 9, Dp = 32;
function Op(e) {
	let t = String(e), n = /\r?\n|\r/g, r = n.exec(t), i = 0, a = [];
	for (; r;) a.push(kp(t.slice(i, r.index), i > 0, !0), r[0]), i = r.index + r[0].length, r = n.exec(t);
	return a.push(kp(t.slice(i), i > 0, !1)), a.join("");
}
function kp(e, t, n) {
	let r = 0, i = e.length;
	if (t) {
		let t = e.codePointAt(r);
		for (; t === Ep || t === Dp;) r++, t = e.codePointAt(r);
	}
	if (n) {
		let t = e.codePointAt(i - 1);
		for (; t === Ep || t === Dp;) i--, t = e.codePointAt(i - 1);
	}
	return i > r ? e.slice(r, i) : "";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/text.js
function Ap(e, t) {
	let n = {
		type: "text",
		value: Op(String(t.value))
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/thematic-break.js
function jp(e, t) {
	let n = {
		type: "element",
		tagName: "hr",
		properties: {},
		children: []
	};
	return e.patch(t, n), e.applyData(t, n);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/handlers/index.js
var Mp = {
	blockquote: np,
	break: rp,
	code: ip,
	delete: ap,
	emphasis: op,
	footnoteReference: sp,
	heading: cp,
	html: lp,
	imageReference: dp,
	image: fp,
	inlineCode: pp,
	linkReference: mp,
	link: hp,
	listItem: gp,
	list: yp,
	paragraph: bp,
	root: xp,
	strong: Sp,
	table: Cp,
	tableCell: Tp,
	tableRow: wp,
	text: Ap,
	thematicBreak: jp,
	toml: Np,
	yaml: Np,
	definition: Np,
	footnoteDefinition: Np
};
function Np() {}
//#endregion
//#region frontend/node_modules/@ungap/structured-clone/esm/deserialize.js
var { defineProperty: Pp } = Object, Fp = typeof self == "object" ? self : globalThis, Ip = (e, t) => {
	switch (e) {
		case "Function":
		case "SharedWorker":
		case "Worker":
		case "eval":
		case "setInterval":
		case "setTimeout": throw TypeError("unable to deserialize " + e);
	}
	return new Fp[e](t);
}, Lp = (e, t) => {
	let n = (t, n) => (e.set(n, t), t), r = (i) => {
		if (e.has(i)) return e.get(i);
		let [a, o] = t[i];
		switch (a) {
			case 0:
			case -1: return n(o, i);
			case 1: {
				let e = n([], i);
				for (let t of o) e.push(r(t));
				return e;
			}
			case 2: {
				let e = n({}, i);
				for (let [t, n] of o) {
					let i = r(t), a = r(n);
					i === "__proto__" ? Pp(e, i, {
						value: a,
						configurable: !0,
						enumerable: !0,
						writable: !0
					}) : e[i] = a;
				}
				return e;
			}
			case 3: return n(new Date(o), i);
			case 4: {
				let { source: e, flags: t } = o;
				return n(new RegExp(e, t), i);
			}
			case 5: {
				let e = n(/* @__PURE__ */ new Map(), i);
				for (let [t, n] of o) e.set(r(t), r(n));
				return e;
			}
			case 6: {
				let e = n(/* @__PURE__ */ new Set(), i);
				for (let t of o) e.add(r(t));
				return e;
			}
			case 7: {
				let { name: e, message: t } = o;
				return n(typeof Fp[e] == "function" ? Ip(e, t) : Error(t), i);
			}
			case 8: return n(BigInt(o), i);
			case "BigInt": return n(Object(BigInt(o)), i);
			case "ArrayBuffer": return n(new Uint8Array(o).buffer, o);
			case "DataView": {
				let { buffer: e } = new Uint8Array(o);
				return n(new DataView(e), o);
			}
			case "-0": return -0;
		}
		return n(Ip(a, o), i);
	};
	return r;
}, Rp = (e) => Lp(/* @__PURE__ */ new Map(), e)(0), zp = "", { toString: Bp } = {}, { keys: Vp, is: Hp } = Object, Up = (e) => {
	let t = typeof e;
	if (t !== "object" || !e) return [0, t];
	let n = Bp.call(e).slice(8, -1);
	switch (n) {
		case "Array": return [1, zp];
		case "Object": return [2, zp];
		case "Date": return [3, zp];
		case "RegExp": return [4, zp];
		case "Map": return [5, zp];
		case "Set": return [6, zp];
		case "DataView": return [1, n];
	}
	return n.includes("Array") ? [1, n] : e instanceof Error ? [7, e.name || "Error"] : [2, n];
}, Wp = ([e, t]) => e === 0 && (t === "function" || t === "symbol"), Gp = (e, t, n, r) => {
	let i = (e, t) => {
		let i = r.push(e) - 1;
		return n.set(t, i), i;
	}, a = (o) => {
		if (n.has(o)) return n.get(o);
		let [s, c] = Up(o);
		switch (s) {
			case 0: {
				let t = o;
				switch (c) {
					case "bigint":
						s = 8, t = o.toString();
						break;
					case "number":
						if (!o && Hp(o, -0)) return r.push(["-0"]) - 1;
						break;
					case "function":
					case "symbol":
						if (e) throw TypeError("unable to serialize " + c);
						t = null;
						break;
					case "undefined": return i([-1], o);
				}
				return i([s, t], o);
			}
			case 1: {
				if (c) {
					let e = o;
					return c === "DataView" ? e = new Uint8Array(o.buffer) : c === "ArrayBuffer" && (e = new Uint8Array(o)), i([c, [...e]], o);
				}
				let e = [], t = i([s, e], o);
				for (let t of o) e.push(a(t));
				return t;
			}
			case 2: {
				if (c) switch (c) {
					case "BigInt": return i([c, o.toString()], o);
					case "Boolean":
					case "Number":
					case "String": return i([c, o.valueOf()], o);
				}
				if (t && "toJSON" in o) return a(o.toJSON());
				let n = [], r = i([s, n], o);
				for (let t of Vp(o)) (e || !Wp(Up(o[t]))) && n.push([a(t), a(o[t])]);
				return r;
			}
			case 3: return i([s, isNaN(o.getTime()) ? zp : o.toISOString()], o);
			case 4: {
				let { source: e, flags: t } = o;
				return i([s, {
					source: e,
					flags: t
				}], o);
			}
			case 5: {
				let t = [], n = i([s, t], o);
				for (let [n, r] of o) (e || !(Wp(Up(n)) || Wp(Up(r)))) && t.push([a(n), a(r)]);
				return n;
			}
			case 6: {
				let t = [], n = i([s, t], o);
				for (let n of o) (e || !Wp(Up(n))) && t.push(a(n));
				return n;
			}
		}
		let { message: l } = o;
		return i([s, {
			name: c,
			message: l
		}], o);
	};
	return a;
}, Kp = (e, { json: t, lossy: n } = {}) => {
	let r = [];
	return Gp(!(t || n), !!t, /* @__PURE__ */ new Map(), r)(e), r;
}, qp = typeof structuredClone == "function" ? (e, t) => t && ("json" in t || "lossy" in t) ? Rp(Kp(e, t)) : structuredClone(e) : (e, t) => Rp(Kp(e, t));
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/footer.js
function Jp(e, t) {
	let n = [{
		type: "text",
		value: "↩"
	}];
	return t > 1 && n.push({
		type: "element",
		tagName: "sup",
		properties: {},
		children: [{
			type: "text",
			value: String(t)
		}]
	}), n;
}
function Yp(e, t) {
	return "Back to reference " + (e + 1) + (t > 1 ? "-" + t : "");
}
function Xp(e) {
	let t = typeof e.options.clobberPrefix == "string" ? e.options.clobberPrefix : "user-content-", n = e.options.footnoteBackContent || Jp, r = e.options.footnoteBackLabel || Yp, i = e.options.footnoteLabel || "Footnotes", a = e.options.footnoteLabelTagName || "h2", o = e.options.footnoteLabelProperties || { className: ["sr-only"] }, s = [], c = -1;
	for (; ++c < e.footnoteOrder.length;) {
		let i = e.footnoteById.get(e.footnoteOrder[c]);
		if (!i) continue;
		let a = e.all(i), o = String(i.identifier).toUpperCase(), l = Du(o.toLowerCase()), u = 0, d = [], f = e.footnoteCounts.get(o);
		for (; f !== void 0 && ++u <= f;) {
			d.length > 0 && d.push({
				type: "text",
				value: " "
			});
			let e = typeof n == "string" ? n : n(c, u);
			typeof e == "string" && (e = {
				type: "text",
				value: e
			}), d.push({
				type: "element",
				tagName: "a",
				properties: {
					href: "#" + t + "fnref-" + l + (u > 1 ? "-" + u : ""),
					dataFootnoteBackref: "",
					ariaLabel: typeof r == "string" ? r : r(c, u),
					className: ["data-footnote-backref"]
				},
				children: Array.isArray(e) ? e : [e]
			});
		}
		let p = a[a.length - 1];
		if (p && p.type === "element" && p.tagName === "p") {
			let e = p.children[p.children.length - 1];
			e && e.type === "text" ? e.value += " " : p.children.push({
				type: "text",
				value: " "
			}), p.children.push(...d);
		} else a.push(...d);
		let m = {
			type: "element",
			tagName: "li",
			properties: { id: t + "fn-" + l },
			children: e.wrap(a, !0)
		};
		e.patch(i, m), s.push(m);
	}
	if (s.length !== 0) return {
		type: "element",
		tagName: "section",
		properties: {
			dataFootnotes: !0,
			className: ["footnotes"]
		},
		children: [
			{
				type: "element",
				tagName: a,
				properties: {
					...qp(o),
					id: "footnote-label"
				},
				children: [{
					type: "text",
					value: i
				}]
			},
			{
				type: "text",
				value: "\n"
			},
			{
				type: "element",
				tagName: "ol",
				properties: {},
				children: e.wrap(s, !0)
			},
			{
				type: "text",
				value: "\n"
			}
		]
	};
}
//#endregion
//#region frontend/node_modules/unist-util-visit/lib/index.js
function Zp(e, t, n, r) {
	let i, a, o;
	typeof t == "function" && typeof n != "function" ? (a = void 0, o = t, i = n) : (a = t, o = n, i = r), $c(e, a, s, i);
	function s(e, t) {
		let n = t[t.length - 1], r = n ? n.children.indexOf(e) : void 0;
		return o(e, r, n);
	}
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/state.js
var Qp = {}.hasOwnProperty, $p = {};
function em(e, t) {
	let n = t || $p, r = /* @__PURE__ */ new Map(), i = /* @__PURE__ */ new Map(), a = {
		all: s,
		applyData: nm,
		definitionById: r,
		footnoteById: i,
		footnoteCounts: /* @__PURE__ */ new Map(),
		footnoteOrder: [],
		handlers: {
			...Mp,
			...n.handlers
		},
		one: o,
		options: n,
		patch: tm,
		wrap: im
	};
	return Zp(e, function(e) {
		if (e.type === "definition" || e.type === "footnoteDefinition") {
			let t = e.type === "definition" ? r : i, n = String(e.identifier).toUpperCase();
			t.has(n) || t.set(n, e);
		}
	}), a;
	function o(e, t) {
		let n = e.type, r = a.handlers[n];
		if (Qp.call(a.handlers, n) && r) return r(a, e, t);
		if (a.options.passThrough && a.options.passThrough.includes(n)) {
			if ("children" in e) {
				let { children: t, ...n } = e, r = qp(n);
				return r.children = a.all(e), r;
			}
			return qp(e);
		}
		return (a.options.unknownHandler || rm)(a, e, t);
	}
	function s(e) {
		let t = [];
		if ("children" in e) {
			let n = e.children, r = -1;
			for (; ++r < n.length;) {
				let i = a.one(n[r], e);
				if (i) {
					if (r && n[r - 1].type === "break" && (!Array.isArray(i) && i.type === "text" && (i.value = am(i.value)), !Array.isArray(i) && i.type === "element")) {
						let e = i.children[0];
						e && e.type === "text" && (e.value = am(e.value));
					}
					Array.isArray(i) ? t.push(...i) : t.push(i);
				}
			}
		}
		return t;
	}
}
function tm(e, t) {
	e.position && (t.position = vl(e));
}
function nm(e, t) {
	let n = t;
	if (e && e.data) {
		let t = e.data.hName, r = e.data.hChildren, i = e.data.hProperties;
		typeof t == "string" && (n.type === "element" ? n.tagName = t : n = {
			type: "element",
			tagName: t,
			properties: {},
			children: "children" in n ? n.children : [n]
		}), n.type === "element" && i && Object.assign(n.properties, qp(i)), "children" in n && n.children && r != null && (n.children = r);
	}
	return n;
}
function rm(e, t) {
	let n = t.data || {}, r = "value" in t && !(Qp.call(n, "hProperties") || Qp.call(n, "hChildren")) ? {
		type: "text",
		value: t.value
	} : {
		type: "element",
		tagName: "div",
		properties: {},
		children: e.all(t)
	};
	return e.patch(t, r), e.applyData(t, r);
}
function im(e, t) {
	let n = [], r = -1;
	for (t && n.push({
		type: "text",
		value: "\n"
	}); ++r < e.length;) r && n.push({
		type: "text",
		value: "\n"
	}), n.push(e[r]);
	return t && e.length > 0 && n.push({
		type: "text",
		value: "\n"
	}), n;
}
function am(e) {
	let t = 0, n = e.charCodeAt(t);
	for (; n === 9 || n === 32;) t++, n = e.charCodeAt(t);
	return e.slice(t);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-hast/lib/index.js
function om(e, t) {
	let n = em(e, t), r = n.one(e, void 0), i = Xp(n), a = Array.isArray(r) ? {
		type: "root",
		children: r
	} : r || {
		type: "root",
		children: []
	};
	return i && ("children" in a, a.children.push({
		type: "text",
		value: "\n"
	}, i)), a;
}
//#endregion
//#region frontend/node_modules/remark-rehype/lib/index.js
function sm(e, t) {
	return e && "run" in e ? async function(n, r) {
		let i = om(n, {
			file: r,
			...t
		});
		await e.run(i, r);
	} : function(n, r) {
		return om(n, {
			file: r,
			...e || t
		});
	};
}
//#endregion
//#region frontend/node_modules/bail/index.js
function cm(e) {
	if (e) throw e;
}
//#endregion
//#region frontend/node_modules/extend/index.js
var lm = /* @__PURE__ */ o(((e, t) => {
	var n = Object.prototype.hasOwnProperty, r = Object.prototype.toString, i = Object.defineProperty, a = Object.getOwnPropertyDescriptor, o = function(e) {
		return typeof Array.isArray == "function" ? Array.isArray(e) : r.call(e) === "[object Array]";
	}, s = function(e) {
		if (!e || r.call(e) !== "[object Object]") return !1;
		var t = n.call(e, "constructor"), i = e.constructor && e.constructor.prototype && n.call(e.constructor.prototype, "isPrototypeOf");
		if (e.constructor && !t && !i) return !1;
		for (var a in e);
		return a === void 0 || n.call(e, a);
	}, c = function(e, t) {
		i && t.name === "__proto__" ? i(e, t.name, {
			enumerable: !0,
			configurable: !0,
			value: t.newValue,
			writable: !0
		}) : e[t.name] = t.newValue;
	}, l = function(e, t) {
		if (t === "__proto__") {
			if (!n.call(e, t)) return;
			if (a) return a(e, t).value;
		}
		return e[t];
	};
	t.exports = function e() {
		var t, n, r, i, a, u, d = arguments[0], f = 1, p = arguments.length, m = !1;
		for (typeof d == "boolean" && (m = d, d = arguments[1] || {}, f = 2), (d == null || typeof d != "object" && typeof d != "function") && (d = {}); f < p; ++f) if (t = arguments[f], t != null) for (n in t) r = l(d, n), i = l(t, n), d !== i && (m && i && (s(i) || (a = o(i))) ? (a ? (a = !1, u = r && o(r) ? r : []) : u = r && s(r) ? r : {}, c(d, {
			name: n,
			newValue: e(m, u, i)
		})) : i !== void 0 && c(d, {
			name: n,
			newValue: i
		}));
		return d;
	};
}));
//#endregion
//#region frontend/node_modules/is-plain-obj/index.js
function um(e) {
	if (typeof e != "object" || !e) return !1;
	let t = Object.getPrototypeOf(e);
	return (t === null || t === Object.prototype || Object.getPrototypeOf(t) === null) && !(Symbol.toStringTag in e) && !(Symbol.iterator in e);
}
//#endregion
//#region frontend/node_modules/trough/lib/index.js
function dm() {
	let e = [], t = {
		run: n,
		use: r
	};
	return t;
	function n(...t) {
		let n = -1, r = t.pop();
		if (typeof r != "function") throw TypeError("Expected function as last argument, not " + r);
		i(null, ...t);
		function i(a, ...o) {
			let s = e[++n], c = -1;
			if (a) {
				r(a);
				return;
			}
			for (; ++c < t.length;) (o[c] === null || o[c] === void 0) && (o[c] = t[c]);
			t = o, s ? fm(s, i)(...o) : r(null, ...o);
		}
	}
	function r(n) {
		if (typeof n != "function") throw TypeError("Expected `middelware` to be a function, not " + n);
		return e.push(n), t;
	}
}
function fm(e, t) {
	let n;
	return r;
	function r(...t) {
		let r = e.length > t.length, o;
		r && t.push(i);
		try {
			o = e.apply(this, t);
		} catch (e) {
			let t = e;
			if (r && n) throw t;
			return i(t);
		}
		r || (o && o.then && typeof o.then == "function" ? o.then(a, i) : o instanceof Error ? i(o) : a(o));
	}
	function i(e, ...r) {
		n || (n = !0, t(e, ...r));
	}
	function a(e) {
		i(null, e);
	}
}
//#endregion
//#region frontend/node_modules/vfile/lib/minpath.browser.js
var pm = {
	basename: mm,
	dirname: hm,
	extname: gm,
	join: _m,
	sep: "/"
};
function mm(e, t) {
	if (t !== void 0 && typeof t != "string") throw TypeError("\"ext\" argument must be a string");
	bm(e);
	let n = 0, r = -1, i = e.length, a;
	if (t === void 0 || t.length === 0 || t.length > e.length) {
		for (; i--;) if (e.codePointAt(i) === 47) {
			if (a) {
				n = i + 1;
				break;
			}
		} else r < 0 && (a = !0, r = i + 1);
		return r < 0 ? "" : e.slice(n, r);
	}
	if (t === e) return "";
	let o = -1, s = t.length - 1;
	for (; i--;) if (e.codePointAt(i) === 47) {
		if (a) {
			n = i + 1;
			break;
		}
	} else o < 0 && (a = !0, o = i + 1), s > -1 && (e.codePointAt(i) === t.codePointAt(s--) ? s < 0 && (r = i) : (s = -1, r = o));
	return n === r ? r = o : r < 0 && (r = e.length), e.slice(n, r);
}
function hm(e) {
	if (bm(e), e.length === 0) return ".";
	let t = -1, n = e.length, r;
	for (; --n;) if (e.codePointAt(n) === 47) {
		if (r) {
			t = n;
			break;
		}
	} else r ||= !0;
	return t < 0 ? e.codePointAt(0) === 47 ? "/" : "." : t === 1 && e.codePointAt(0) === 47 ? "//" : e.slice(0, t);
}
function gm(e) {
	bm(e);
	let t = e.length, n = -1, r = 0, i = -1, a = 0, o;
	for (; t--;) {
		let s = e.codePointAt(t);
		if (s === 47) {
			if (o) {
				r = t + 1;
				break;
			}
			continue;
		}
		n < 0 && (o = !0, n = t + 1), s === 46 ? i < 0 ? i = t : a !== 1 && (a = 1) : i > -1 && (a = -1);
	}
	return i < 0 || n < 0 || a === 0 || a === 1 && i === n - 1 && i === r + 1 ? "" : e.slice(i, n);
}
function _m(...e) {
	let t = -1, n;
	for (; ++t < e.length;) bm(e[t]), e[t] && (n = n === void 0 ? e[t] : n + "/" + e[t]);
	return n === void 0 ? "." : vm(n);
}
function vm(e) {
	bm(e);
	let t = e.codePointAt(0) === 47, n = ym(e, !t);
	return n.length === 0 && !t && (n = "."), n.length > 0 && e.codePointAt(e.length - 1) === 47 && (n += "/"), t ? "/" + n : n;
}
function ym(e, t) {
	let n = "", r = 0, i = -1, a = 0, o = -1, s, c;
	for (; ++o <= e.length;) {
		if (o < e.length) s = e.codePointAt(o);
		else if (s === 47) break;
		else s = 47;
		if (s === 47) {
			if (!(i === o - 1 || a === 1)) if (i !== o - 1 && a === 2) {
				if (n.length < 2 || r !== 2 || n.codePointAt(n.length - 1) !== 46 || n.codePointAt(n.length - 2) !== 46) {
					if (n.length > 2) {
						if (c = n.lastIndexOf("/"), c !== n.length - 1) {
							c < 0 ? (n = "", r = 0) : (n = n.slice(0, c), r = n.length - 1 - n.lastIndexOf("/")), i = o, a = 0;
							continue;
						}
					} else if (n.length > 0) {
						n = "", r = 0, i = o, a = 0;
						continue;
					}
				}
				t && (n = n.length > 0 ? n + "/.." : "..", r = 2);
			} else n.length > 0 ? n += "/" + e.slice(i + 1, o) : n = e.slice(i + 1, o), r = o - i - 1;
			i = o, a = 0;
		} else s === 46 && a > -1 ? a++ : a = -1;
	}
	return n;
}
function bm(e) {
	if (typeof e != "string") throw TypeError("Path must be a string. Received " + JSON.stringify(e));
}
//#endregion
//#region frontend/node_modules/vfile/lib/minproc.browser.js
var xm = { cwd: Sm };
function Sm() {
	return "/";
}
//#endregion
//#region frontend/node_modules/vfile/lib/minurl.shared.js
function Cm(e) {
	return !!(typeof e == "object" && e && "href" in e && e.href && "protocol" in e && e.protocol && e.auth === void 0);
}
//#endregion
//#region frontend/node_modules/vfile/lib/minurl.browser.js
function wm(e) {
	if (typeof e == "string") e = new URL(e);
	else if (!Cm(e)) {
		let t = /* @__PURE__ */ TypeError("The \"path\" argument must be of type string or an instance of URL. Received `" + e + "`");
		throw t.code = "ERR_INVALID_ARG_TYPE", t;
	}
	if (e.protocol !== "file:") {
		let e = /* @__PURE__ */ TypeError("The URL must be of scheme file");
		throw e.code = "ERR_INVALID_URL_SCHEME", e;
	}
	return Tm(e);
}
function Tm(e) {
	if (e.hostname !== "") {
		let e = /* @__PURE__ */ TypeError("File URL host must be \"localhost\" or empty on darwin");
		throw e.code = "ERR_INVALID_FILE_URL_HOST", e;
	}
	let t = e.pathname, n = -1;
	for (; ++n < t.length;) if (t.codePointAt(n) === 37 && t.codePointAt(n + 1) === 50) {
		let e = t.codePointAt(n + 2);
		if (e === 70 || e === 102) {
			let e = /* @__PURE__ */ TypeError("File URL path must not include encoded / characters");
			throw e.code = "ERR_INVALID_FILE_URL_PATH", e;
		}
	}
	return decodeURIComponent(t);
}
//#endregion
//#region frontend/node_modules/vfile/lib/index.js
var Em = [
	"history",
	"path",
	"basename",
	"stem",
	"extname",
	"dirname"
], Dm = class {
	constructor(e) {
		let t;
		t = e ? Cm(e) ? { path: e } : typeof e == "string" || jm(e) ? { value: e } : e : {}, this.cwd = "cwd" in t ? "" : xm.cwd(), this.data = {}, this.history = [], this.messages = [], this.value, this.map, this.result, this.stored;
		let n = -1;
		for (; ++n < Em.length;) {
			let e = Em[n];
			e in t && t[e] !== void 0 && t[e] !== null && (this[e] = e === "history" ? [...t[e]] : t[e]);
		}
		let r;
		for (r in t) Em.includes(r) || (this[r] = t[r]);
	}
	get basename() {
		return typeof this.path == "string" ? pm.basename(this.path) : void 0;
	}
	set basename(e) {
		km(e, "basename"), Om(e, "basename"), this.path = pm.join(this.dirname || "", e);
	}
	get dirname() {
		return typeof this.path == "string" ? pm.dirname(this.path) : void 0;
	}
	set dirname(e) {
		Am(this.basename, "dirname"), this.path = pm.join(e || "", this.basename);
	}
	get extname() {
		return typeof this.path == "string" ? pm.extname(this.path) : void 0;
	}
	set extname(e) {
		if (Om(e, "extname"), Am(this.dirname, "extname"), e) {
			if (e.codePointAt(0) !== 46) throw Error("`extname` must start with `.`");
			if (e.includes(".", 1)) throw Error("`extname` cannot contain multiple dots");
		}
		this.path = pm.join(this.dirname, this.stem + (e || ""));
	}
	get path() {
		return this.history[this.history.length - 1];
	}
	set path(e) {
		Cm(e) && (e = wm(e)), km(e, "path"), this.path !== e && this.history.push(e);
	}
	get stem() {
		return typeof this.path == "string" ? pm.basename(this.path, this.extname) : void 0;
	}
	set stem(e) {
		km(e, "stem"), Om(e, "stem"), this.path = pm.join(this.dirname || "", e + (this.extname || ""));
	}
	fail(e, t, n) {
		let r = this.message(e, t, n);
		throw r.fatal = !0, r;
	}
	info(e, t, n) {
		let r = this.message(e, t, n);
		return r.fatal = void 0, r;
	}
	message(e, t, n) {
		let r = new Cl(e, t, n);
		return this.path && (r.name = this.path + ":" + r.name, r.file = this.path), r.fatal = !1, this.messages.push(r), r;
	}
	toString(e) {
		return this.value === void 0 ? "" : typeof this.value == "string" ? this.value : new TextDecoder(e || void 0).decode(this.value);
	}
};
function Om(e, t) {
	if (e && e.includes(pm.sep)) throw Error("`" + t + "` cannot be a path: did not expect `" + pm.sep + "`");
}
function km(e, t) {
	if (!e) throw Error("`" + t + "` cannot be empty");
}
function Am(e, t) {
	if (!e) throw Error("Setting `" + t + "` requires `path` to be set too");
}
function jm(e) {
	return !!(e && typeof e == "object" && "byteLength" in e && "byteOffset" in e);
}
//#endregion
//#region frontend/node_modules/unified/lib/callable-instance.js
var Mm = (function(e) {
	let t = this.constructor.prototype, n = t[e], r = function() {
		return n.apply(r, arguments);
	};
	return Object.setPrototypeOf(r, t), r;
}), Nm = /* @__PURE__ */ l(lm(), 1), Pm = {}.hasOwnProperty, Fm = new class e extends Mm {
	constructor() {
		super("copy"), this.Compiler = void 0, this.Parser = void 0, this.attachers = [], this.compiler = void 0, this.freezeIndex = -1, this.frozen = void 0, this.namespace = {}, this.parser = void 0, this.transformers = dm();
	}
	copy() {
		let t = new e(), n = -1;
		for (; ++n < this.attachers.length;) {
			let e = this.attachers[n];
			t.use(...e);
		}
		return t.data((0, Nm.default)(!0, {}, this.namespace)), t;
	}
	data(e, t) {
		return typeof e == "string" ? arguments.length === 2 ? (Rm("data", this.frozen), this.namespace[e] = t, this) : Pm.call(this.namespace, e) && this.namespace[e] || void 0 : e ? (Rm("data", this.frozen), this.namespace = e, this) : this.namespace;
	}
	freeze() {
		if (this.frozen) return this;
		let e = this;
		for (; ++this.freezeIndex < this.attachers.length;) {
			let [t, ...n] = this.attachers[this.freezeIndex];
			if (n[0] === !1) continue;
			n[0] === !0 && (n[0] = void 0);
			let r = t.call(e, ...n);
			typeof r == "function" && this.transformers.use(r);
		}
		return this.frozen = !0, this.freezeIndex = Infinity, this;
	}
	parse(e) {
		this.freeze();
		let t = Vm(e), n = this.parser || this.Parser;
		return Im("parse", n), n(String(t), t);
	}
	process(e, t) {
		let n = this;
		return this.freeze(), Im("process", this.parser || this.Parser), Lm("process", this.compiler || this.Compiler), t ? r(void 0, t) : new Promise(r);
		function r(r, i) {
			let a = Vm(e), o = n.parse(a);
			n.run(o, a, function(e, t, r) {
				if (e || !t || !r) return s(e);
				let i = t, a = n.stringify(i, r);
				Um(a) ? r.value = a : r.result = a, s(e, r);
			});
			function s(e, n) {
				e || !n ? i(e) : r ? r(n) : t(void 0, n);
			}
		}
	}
	processSync(e) {
		let t = !1, n;
		return this.freeze(), Im("processSync", this.parser || this.Parser), Lm("processSync", this.compiler || this.Compiler), this.process(e, r), Bm("processSync", "process", t), n;
		function r(e, r) {
			t = !0, cm(e), n = r;
		}
	}
	run(e, t, n) {
		zm(e), this.freeze();
		let r = this.transformers;
		return !n && typeof t == "function" && (n = t, t = void 0), n ? i(void 0, n) : new Promise(i);
		function i(i, a) {
			let o = Vm(t);
			r.run(e, o, s);
			function s(t, r, o) {
				let s = r || e;
				t ? a(t) : i ? i(s) : n(void 0, s, o);
			}
		}
	}
	runSync(e, t) {
		let n = !1, r;
		return this.run(e, t, i), Bm("runSync", "run", n), r;
		function i(e, t) {
			cm(e), r = t, n = !0;
		}
	}
	stringify(e, t) {
		this.freeze();
		let n = Vm(t), r = this.compiler || this.Compiler;
		return Lm("stringify", r), zm(e), r(e, n);
	}
	use(e, ...t) {
		let n = this.attachers, r = this.namespace;
		if (Rm("use", this.frozen), e != null) if (typeof e == "function") s(e, t);
		else if (typeof e == "object") Array.isArray(e) ? o(e) : a(e);
		else throw TypeError("Expected usable value, not `" + e + "`");
		return this;
		function i(e) {
			if (typeof e == "function") s(e, []);
			else if (typeof e == "object") if (Array.isArray(e)) {
				let [t, ...n] = e;
				s(t, n);
			} else a(e);
			else throw TypeError("Expected usable value, not `" + e + "`");
		}
		function a(e) {
			if (!("plugins" in e) && !("settings" in e)) throw Error("Expected usable value but received an empty preset, which is probably a mistake: presets typically come with `plugins` and sometimes with `settings`, but this has neither");
			o(e.plugins), e.settings && (r.settings = (0, Nm.default)(!0, r.settings, e.settings));
		}
		function o(e) {
			let t = -1;
			if (e != null) if (Array.isArray(e)) for (; ++t < e.length;) {
				let n = e[t];
				i(n);
			}
			else throw TypeError("Expected a list of plugins, not `" + e + "`");
		}
		function s(e, t) {
			let r = -1, i = -1;
			for (; ++r < n.length;) if (n[r][0] === e) {
				i = r;
				break;
			}
			if (i === -1) n.push([e, ...t]);
			else if (t.length > 0) {
				let [r, ...a] = t, o = n[i][1];
				um(o) && um(r) && (r = (0, Nm.default)(!0, o, r)), n[i] = [
					e,
					r,
					...a
				];
			}
		}
	}
}().freeze();
function Im(e, t) {
	if (typeof t != "function") throw TypeError("Cannot `" + e + "` without `parser`");
}
function Lm(e, t) {
	if (typeof t != "function") throw TypeError("Cannot `" + e + "` without `compiler`");
}
function Rm(e, t) {
	if (t) throw Error("Cannot call `" + e + "` on a frozen processor.\nCreate a new processor first, by calling it: use `processor()` instead of `processor`.");
}
function zm(e) {
	if (!um(e) || typeof e.type != "string") throw TypeError("Expected node, got `" + e + "`");
}
function Bm(e, t, n) {
	if (!n) throw Error("`" + e + "` finished async. Use `" + t + "` instead");
}
function Vm(e) {
	return Hm(e) ? e : new Dm(e);
}
function Hm(e) {
	return !!(e && typeof e == "object" && "message" in e && "messages" in e);
}
function Um(e) {
	return typeof e == "string" || Wm(e);
}
function Wm(e) {
	return !!(e && typeof e == "object" && "byteLength" in e && "byteOffset" in e);
}
//#endregion
//#region frontend/node_modules/react-markdown/lib/index.js
var Gm = [], Km = { allowDangerousHtml: !0 }, qm = /^(https?|ircs?|mailto|xmpp)$/i, Jm = [
	{
		from: "astPlugins",
		id: "remove-buggy-html-in-markdown-parser"
	},
	{
		from: "allowDangerousHtml",
		id: "remove-buggy-html-in-markdown-parser"
	},
	{
		from: "allowNode",
		id: "replace-allownode-allowedtypes-and-disallowedtypes",
		to: "allowElement"
	},
	{
		from: "allowedTypes",
		id: "replace-allownode-allowedtypes-and-disallowedtypes",
		to: "allowedElements"
	},
	{
		from: "className",
		id: "remove-classname"
	},
	{
		from: "disallowedTypes",
		id: "replace-allownode-allowedtypes-and-disallowedtypes",
		to: "disallowedElements"
	},
	{
		from: "escapeHtml",
		id: "remove-buggy-html-in-markdown-parser"
	},
	{
		from: "includeElementIndex",
		id: "#remove-includeelementindex"
	},
	{
		from: "includeNodeIndex",
		id: "change-includenodeindex-to-includeelementindex"
	},
	{
		from: "linkTarget",
		id: "remove-linktarget"
	},
	{
		from: "plugins",
		id: "change-plugins-to-remarkplugins",
		to: "remarkPlugins"
	},
	{
		from: "rawSourcePos",
		id: "#remove-rawsourcepos"
	},
	{
		from: "renderers",
		id: "change-renderers-to-components",
		to: "components"
	},
	{
		from: "source",
		id: "change-source-to-children",
		to: "children"
	},
	{
		from: "sourcePos",
		id: "#remove-sourcepos"
	},
	{
		from: "transformImageUri",
		id: "#add-urltransform",
		to: "urlTransform"
	},
	{
		from: "transformLinkUri",
		id: "#add-urltransform",
		to: "urlTransform"
	}
];
function Ym(e) {
	let t = Xm(e), n = Zm(e);
	return Qm(t.runSync(t.parse(n), n), e);
}
function Xm(e) {
	let t = e.rehypePlugins || Gm, n = e.remarkPlugins || Gm, r = e.remarkRehypeOptions ? {
		...e.remarkRehypeOptions,
		...Km
	} : Km;
	return Fm().use(tp).use(n).use(sm, r).use(t);
}
function Zm(e) {
	let t = e.children || "", n = new Dm();
	return typeof t == "string" ? n.value = t : "" + t, n;
}
function Qm(e, t) {
	let n = t.allowedElements, r = t.allowElement, i = t.components, a = t.disallowedElements, o = t.skipHtml, s = t.unwrapDisallowed, c = t.urlTransform || $m;
	for (let e of Jm) Object.hasOwn(t, e.from) && "" + e.from + (e.to ? "use `" + e.to + "` instead" : "remove it") + e.id;
	return Zp(e, l), jl(e, {
		Fragment: A.Fragment,
		components: i,
		ignoreInvalidStyle: !0,
		jsx: A.jsx,
		jsxs: A.jsxs,
		passKeys: !0,
		passNode: !0
	});
	function l(e, t, i) {
		if (e.type === "raw" && i && typeof t == "number") return o ? i.children.splice(t, 1) : i.children[t] = {
			type: "text",
			value: e.value
		}, t;
		if (e.type === "element") {
			let t;
			for (t in $l) if (Object.hasOwn($l, t) && Object.hasOwn(e.properties, t)) {
				let n = e.properties[t], r = $l[t];
				(r === null || r.includes(e.tagName)) && (e.properties[t] = c(String(n || ""), t, e));
			}
		}
		if (e.type === "element") {
			let o = n ? !n.includes(e.tagName) : a ? a.includes(e.tagName) : !1;
			if (!o && r && typeof t == "number" && (o = !r(e, t, i)), o && i && typeof t == "number") return s && e.children ? i.children.splice(t, 1, ...e.children) : i.children.splice(t, 1), t;
		}
	}
}
function $m(e) {
	let t = e.indexOf(":"), n = e.indexOf("?"), r = e.indexOf("#"), i = e.indexOf("/");
	return t === -1 || i !== -1 && t > i || n !== -1 && t > n || r !== -1 && t > r || qm.test(e.slice(0, t)) ? e : "";
}
//#endregion
//#region frontend/node_modules/ccount/index.js
function eh(e, t) {
	let n = String(e);
	if (typeof t != "string") throw TypeError("Expected character");
	let r = 0, i = n.indexOf(t);
	for (; i !== -1;) r++, i = n.indexOf(t, i + t.length);
	return r;
}
//#endregion
//#region frontend/node_modules/mdast-util-find-and-replace/node_modules/escape-string-regexp/index.js
function th(e) {
	if (typeof e != "string") throw TypeError("Expected a string");
	return e.replace(/[|\\{}()[\]^$+*?.]/g, "\\$&").replace(/-/g, "\\x2d");
}
//#endregion
//#region frontend/node_modules/mdast-util-find-and-replace/lib/index.js
function nh(e, t, n) {
	let r = rt((n || {}).ignore || []), i = rh(t), a = -1;
	for (; ++a < i.length;) $c(e, "text", o);
	function o(e, t) {
		let n = -1, i;
		for (; ++n < t.length;) {
			let e = t[n], a = i ? i.children : void 0;
			if (r(e, a ? a.indexOf(e) : void 0, i)) return;
			i = e;
		}
		if (i) return s(e, t);
	}
	function s(e, t) {
		let n = t[t.length - 1], r = i[a][0], o = i[a][1], s = 0, c = n.children.indexOf(e), l = !1, u = [];
		r.lastIndex = 0;
		let d = r.exec(e.value);
		for (; d;) {
			let n = d.index, i = {
				index: d.index,
				input: d.input,
				stack: [...t, e]
			}, a = o(...d, i);
			if (typeof a == "string" && (a = a.length > 0 ? {
				type: "text",
				value: a
			} : void 0), a === !1 ? r.lastIndex = n + 1 : (s !== n && u.push({
				type: "text",
				value: e.value.slice(s, n)
			}), Array.isArray(a) ? u.push(...a) : a && u.push(a), s = n + d[0].length, l = !0), !r.global) break;
			d = r.exec(e.value);
		}
		return l ? (s < e.value.length && u.push({
			type: "text",
			value: e.value.slice(s)
		}), n.children.splice(c, 1, ...u)) : u = [e], c + u.length;
	}
}
function rh(e) {
	let t = [];
	if (!Array.isArray(e)) throw TypeError("Expected find and replace tuple or list of tuples");
	let n = !e[0] || Array.isArray(e[0]) ? e : [e], r = -1;
	for (; ++r < n.length;) {
		let e = n[r];
		t.push([ih(e[0]), ah(e[1])]);
	}
	return t;
}
function ih(e) {
	return typeof e == "string" ? new RegExp(th(e), "g") : e;
}
function ah(e) {
	return typeof e == "function" ? e : function() {
		return e;
	};
}
//#endregion
//#region frontend/node_modules/mdast-util-gfm-autolink-literal/lib/index.js
var oh = "phrasing", sh = [
	"autolink",
	"link",
	"image",
	"label"
];
function ch() {
	return {
		transforms: [gh],
		enter: {
			literalAutolink: uh,
			literalAutolinkEmail: dh,
			literalAutolinkHttp: dh,
			literalAutolinkWww: dh
		},
		exit: {
			literalAutolink: hh,
			literalAutolinkEmail: mh,
			literalAutolinkHttp: fh,
			literalAutolinkWww: ph
		}
	};
}
function lh() {
	return { unsafe: [
		{
			character: "@",
			before: "[+\\-.\\w]",
			after: "[\\-.\\w]",
			inConstruct: oh,
			notInConstruct: sh
		},
		{
			character: ".",
			before: "[Ww]",
			after: "[\\-.\\w]",
			inConstruct: oh,
			notInConstruct: sh
		},
		{
			character: ":",
			before: "[ps]",
			after: "\\/",
			inConstruct: oh,
			notInConstruct: sh
		}
	] };
}
function uh(e) {
	this.enter({
		type: "link",
		title: null,
		url: "",
		children: []
	}, e);
}
function dh(e) {
	this.config.enter.autolinkProtocol.call(this, e);
}
function fh(e) {
	this.config.exit.autolinkProtocol.call(this, e);
}
function ph(e) {
	this.config.exit.data.call(this, e);
	let t = this.stack[this.stack.length - 1];
	t.type, t.url = "http://" + this.sliceSerialize(e);
}
function mh(e) {
	this.config.exit.autolinkEmail.call(this, e);
}
function hh(e) {
	this.exit(e);
}
function gh(e) {
	nh(e, [[/(https?:\/\/|www(?=\.))([-.\w]+)([^ \t\r\n]*)/gi, _h], [/(?<=^|\s|\p{P}|\p{S})([-.\w+]+)@([-\w]+(?:\.[-\w]+)+)/gu, vh]], { ignore: ["link", "linkReference"] });
}
function _h(e, t, n, r, i) {
	let a = "";
	if (!xh(i) || (/^w/i.test(t) && (n = t + n, t = "", a = "http://"), !yh(n))) return !1;
	let o = bh(n + r);
	if (!o[0]) return !1;
	let s = {
		type: "link",
		title: null,
		url: a + t + o[0],
		children: [{
			type: "text",
			value: t + o[0]
		}]
	};
	return o[1] ? [s, {
		type: "text",
		value: o[1]
	}] : s;
}
function vh(e, t, n, r) {
	return !xh(r, !0) || /[-\d_]$/.test(n) ? !1 : {
		type: "link",
		title: null,
		url: "mailto:" + t + "@" + n,
		children: [{
			type: "text",
			value: t + "@" + n
		}]
	};
}
function yh(e) {
	let t = e.split(".");
	return !(t.length < 2 || t[t.length - 1] && (/_/.test(t[t.length - 1]) || !/[a-zA-Z\d]/.test(t[t.length - 1])) || t[t.length - 2] && (/_/.test(t[t.length - 2]) || !/[a-zA-Z\d]/.test(t[t.length - 2])));
}
function bh(e) {
	let t = /[!"&'),.:;<>?\]}]+$/.exec(e);
	if (!t) return [e, void 0];
	e = e.slice(0, t.index);
	let n = t[0], r = n.indexOf(")"), i = eh(e, "("), a = eh(e, ")");
	for (; r !== -1 && i > a;) e += n.slice(0, r + 1), n = n.slice(r + 1), r = n.indexOf(")"), a++;
	return [e, n];
}
function xh(e, t) {
	let n = e.input.charCodeAt(e.index - 1);
	return (e.index === 0 || Tu(n) || wu(n)) && (!t || n !== 47);
}
//#endregion
//#region frontend/node_modules/mdast-util-gfm-footnote/lib/index.js
jh.peek = Ah;
function Sh() {
	this.buffer();
}
function Ch(e) {
	this.enter({
		type: "footnoteReference",
		identifier: "",
		label: ""
	}, e);
}
function wh() {
	this.buffer();
}
function Th(e) {
	this.enter({
		type: "footnoteDefinition",
		identifier: "",
		label: "",
		children: []
	}, e);
}
function Eh(e) {
	let t = this.resume(), n = this.stack[this.stack.length - 1];
	n.type, n.identifier = mu(this.sliceSerialize(e)).toLowerCase(), n.label = t;
}
function Dh(e) {
	this.exit(e);
}
function Oh(e) {
	let t = this.resume(), n = this.stack[this.stack.length - 1];
	n.type, n.identifier = mu(this.sliceSerialize(e)).toLowerCase(), n.label = t;
}
function kh(e) {
	this.exit(e);
}
function Ah() {
	return "[";
}
function jh(e, t, n, r) {
	let i = n.createTracker(r), a = i.move("[^"), o = n.enter("footnoteReference"), s = n.enter("reference");
	return a += i.move(n.safe(n.associationId(e), {
		after: "]",
		before: a
	})), s(), o(), a += i.move("]"), a;
}
function Mh() {
	return {
		enter: {
			gfmFootnoteCallString: Sh,
			gfmFootnoteCall: Ch,
			gfmFootnoteDefinitionLabelString: wh,
			gfmFootnoteDefinition: Th
		},
		exit: {
			gfmFootnoteCallString: Eh,
			gfmFootnoteCall: Dh,
			gfmFootnoteDefinitionLabelString: Oh,
			gfmFootnoteDefinition: kh
		}
	};
}
function Nh(e) {
	let t = !1;
	return e && e.firstLineBlank && (t = !0), {
		handlers: {
			footnoteDefinition: n,
			footnoteReference: jh
		},
		unsafe: [{
			character: "[",
			inConstruct: [
				"label",
				"phrasing",
				"reference"
			]
		}]
	};
	function n(e, n, r, i) {
		let a = r.createTracker(i), o = a.move("[^"), s = r.enter("footnoteDefinition"), c = r.enter("label");
		return o += a.move(r.safe(r.associationId(e), {
			before: o,
			after: "]"
		})), c(), o += a.move("]:"), e.children && e.children.length > 0 && (a.shift(4), o += a.move((t ? "\n" : " ") + r.indentLines(r.containerFlow(e, a.current()), t ? Fh : Ph))), s(), o;
	}
}
function Ph(e, t, n) {
	return t === 0 ? e : Fh(e, t, n);
}
function Fh(e, t, n) {
	return (n ? "" : "    ") + e;
}
//#endregion
//#region frontend/node_modules/mdast-util-gfm-strikethrough/lib/index.js
var Ih = [
	"autolink",
	"destinationLiteral",
	"destinationRaw",
	"reference",
	"titleQuote",
	"titleApostrophe"
];
Vh.peek = Hh;
function Lh() {
	return {
		canContainEols: ["delete"],
		enter: { strikethrough: zh },
		exit: { strikethrough: Bh }
	};
}
function Rh() {
	return {
		unsafe: [{
			character: "~",
			inConstruct: "phrasing",
			notInConstruct: Ih
		}],
		handlers: { delete: Vh }
	};
}
function zh(e) {
	this.enter({
		type: "delete",
		children: []
	}, e);
}
function Bh(e) {
	this.exit(e);
}
function Vh(e, t, n, r) {
	let i = n.createTracker(r), a = n.enter("strikethrough"), o = i.move("~~");
	return o += n.containerPhrasing(e, {
		...i.current(),
		before: o,
		after: "~"
	}), o += i.move("~~"), a(), o;
}
function Hh() {
	return "~";
}
//#endregion
//#region frontend/node_modules/markdown-table/index.js
function Uh(e) {
	return e.length;
}
function Wh(e, t) {
	let n = t || {}, r = (n.align || []).concat(), i = n.stringLength || Uh, a = [], o = [], s = [], c = [], l = 0, u = -1;
	for (; ++u < e.length;) {
		let t = [], r = [], a = -1;
		for (e[u].length > l && (l = e[u].length); ++a < e[u].length;) {
			let o = Gh(e[u][a]);
			if (n.alignDelimiters !== !1) {
				let e = i(o);
				r[a] = e, (c[a] === void 0 || e > c[a]) && (c[a] = e);
			}
			t.push(o);
		}
		o[u] = t, s[u] = r;
	}
	let d = -1;
	if (typeof r == "object" && "length" in r) for (; ++d < l;) a[d] = Kh(r[d]);
	else {
		let e = Kh(r);
		for (; ++d < l;) a[d] = e;
	}
	d = -1;
	let f = [], p = [];
	for (; ++d < l;) {
		let e = a[d], t = "", r = "";
		e === 99 ? (t = ":", r = ":") : e === 108 ? t = ":" : e === 114 && (r = ":");
		let i = n.alignDelimiters === !1 ? 1 : Math.max(1, c[d] - t.length - r.length), o = t + "-".repeat(i) + r;
		n.alignDelimiters !== !1 && (i = t.length + i + r.length, i > c[d] && (c[d] = i), p[d] = i), f[d] = o;
	}
	o.splice(1, 0, f), s.splice(1, 0, p), u = -1;
	let m = [];
	for (; ++u < o.length;) {
		let e = o[u], t = s[u];
		d = -1;
		let r = [];
		for (; ++d < l;) {
			let i = e[d] || "", o = "", s = "";
			if (n.alignDelimiters !== !1) {
				let e = c[d] - (t[d] || 0), n = a[d];
				n === 114 ? o = " ".repeat(e) : n === 99 ? e % 2 ? (o = " ".repeat(e / 2 + .5), s = " ".repeat(e / 2 - .5)) : (o = " ".repeat(e / 2), s = o) : s = " ".repeat(e);
			}
			n.delimiterStart !== !1 && !d && r.push("|"), n.padding !== !1 && !(n.alignDelimiters === !1 && i === "") && (n.delimiterStart !== !1 || d) && r.push(" "), n.alignDelimiters !== !1 && r.push(o), r.push(i), n.alignDelimiters !== !1 && r.push(s), n.padding !== !1 && r.push(" "), (n.delimiterEnd !== !1 || d !== l - 1) && r.push("|");
		}
		m.push(n.delimiterEnd === !1 ? r.join("").replace(/ +$/, "") : r.join(""));
	}
	return m.join("\n");
}
function Gh(e) {
	return e == null ? "" : String(e);
}
function Kh(e) {
	let t = typeof e == "string" ? e.codePointAt(0) : 0;
	return t === 67 || t === 99 ? 99 : t === 76 || t === 108 ? 108 : t === 82 || t === 114 ? 114 : 0;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/blockquote.js
function qh(e, t, n, r) {
	let i = n.enter("blockquote"), a = n.createTracker(r);
	a.move("> "), a.shift(2);
	let o = n.indentLines(n.containerFlow(e, a.current()), Jh);
	return i(), o;
}
function Jh(e, t, n) {
	return ">" + (n ? "" : " ") + e;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/pattern-in-scope.js
function Yh(e, t) {
	return Xh(e, t.inConstruct, !0) && !Xh(e, t.notInConstruct, !1);
}
function Xh(e, t, n) {
	if (typeof t == "string" && (t = [t]), !t || t.length === 0) return n;
	let r = -1;
	for (; ++r < t.length;) if (e.includes(t[r])) return !0;
	return !1;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/break.js
function Zh(e, t, n, r) {
	let i = -1;
	for (; ++i < n.unsafe.length;) if (n.unsafe[i].character === "\n" && Yh(n.stack, n.unsafe[i])) return /[ \t]/.test(r.before) ? "" : " ";
	return "\\\n";
}
//#endregion
//#region frontend/node_modules/longest-streak/index.js
function Qh(e, t) {
	let n = String(e), r = n.indexOf(t), i = r, a = 0, o = 0;
	if (typeof t != "string") throw TypeError("Expected substring");
	for (; r !== -1;) r === i ? ++a > o && (o = a) : a = 1, i = r + t.length, r = n.indexOf(t, i);
	return o;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/format-code-as-indented.js
function $h(e, t) {
	return !!(t.options.fences === !1 && e.value && !e.lang && /[^ \r\n]/.test(e.value) && !/^[\t ]*(?:[\r\n]|$)|(?:^|[\r\n])[\t ]*$/.test(e.value));
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-fence.js
function eg(e) {
	let t = e.options.fence || "`";
	if (t !== "`" && t !== "~") throw Error("Cannot serialize code with `" + t + "` for `options.fence`, expected `` ` `` or `~`");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/code.js
function tg(e, t, n, r) {
	let i = eg(n), a = e.value || "", o = i === "`" ? "GraveAccent" : "Tilde";
	if ($h(e, n)) {
		let e = n.enter("codeIndented"), t = n.indentLines(a, ng);
		return e(), t;
	}
	let s = n.createTracker(r), c = i.repeat(Math.max(Qh(a, i) + 1, 3)), l = n.enter("codeFenced"), u = s.move(c);
	if (e.lang) {
		let t = n.enter(`codeFencedLang${o}`);
		u += s.move(n.safe(e.lang, {
			before: u,
			after: " ",
			encode: ["`"],
			...s.current()
		})), t();
	}
	if (e.lang && e.meta) {
		let t = n.enter(`codeFencedMeta${o}`);
		u += s.move(" "), u += s.move(n.safe(e.meta, {
			before: u,
			after: "\n",
			encode: ["`"],
			...s.current()
		})), t();
	}
	return u += s.move("\n"), a && (u += s.move(a + "\n")), u += s.move(c), l(), u;
}
function ng(e, t, n) {
	return (n ? "" : "    ") + e;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-quote.js
function rg(e) {
	let t = e.options.quote || "\"";
	if (t !== "\"" && t !== "'") throw Error("Cannot serialize title with `" + t + "` for `options.quote`, expected `\"`, or `'`");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/definition.js
function ig(e, t, n, r) {
	let i = rg(n), a = i === "\"" ? "Quote" : "Apostrophe", o = n.enter("definition"), s = n.enter("label"), c = n.createTracker(r), l = c.move("[");
	return l += c.move(n.safe(n.associationId(e), {
		before: l,
		after: "]",
		...c.current()
	})), l += c.move("]: "), s(), !e.url || /[\0- \u007F]/.test(e.url) ? (s = n.enter("destinationLiteral"), l += c.move("<"), l += c.move(n.safe(e.url, {
		before: l,
		after: ">",
		...c.current()
	})), l += c.move(">")) : (s = n.enter("destinationRaw"), l += c.move(n.safe(e.url, {
		before: l,
		after: e.title ? " " : "\n",
		...c.current()
	}))), s(), e.title && (s = n.enter(`title${a}`), l += c.move(" " + i), l += c.move(n.safe(e.title, {
		before: l,
		after: i,
		...c.current()
	})), l += c.move(i), s()), o(), l;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-emphasis.js
function ag(e) {
	let t = e.options.emphasis || "*";
	if (t !== "*" && t !== "_") throw Error("Cannot serialize emphasis with `" + t + "` for `options.emphasis`, expected `*`, or `_`");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/encode-character-reference.js
function og(e) {
	return "&#x" + e.toString(16).toUpperCase() + ";";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/encode-info.js
function sg(e, t, n) {
	let r = Fu(e), i = Fu(t);
	return r === void 0 ? i === void 0 ? n === "_" ? {
		inside: !0,
		outside: !0
	} : {
		inside: !1,
		outside: !1
	} : i === 1 ? {
		inside: !0,
		outside: !0
	} : {
		inside: !1,
		outside: !0
	} : r === 1 ? i === void 0 ? {
		inside: !1,
		outside: !1
	} : i === 1 ? {
		inside: !0,
		outside: !0
	} : {
		inside: !1,
		outside: !1
	} : i === void 0 ? {
		inside: !1,
		outside: !1
	} : i === 1 ? {
		inside: !0,
		outside: !1
	} : {
		inside: !1,
		outside: !1
	};
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/emphasis.js
cg.peek = lg;
function cg(e, t, n, r) {
	let i = ag(n), a = n.enter("emphasis"), o = n.createTracker(r), s = o.move(i), c = o.move(n.containerPhrasing(e, {
		after: i,
		before: s,
		...o.current()
	})), l = c.charCodeAt(0), u = sg(r.before.charCodeAt(r.before.length - 1), l, i);
	u.inside && (c = og(l) + c.slice(1));
	let d = c.charCodeAt(c.length - 1), f = sg(r.after.charCodeAt(0), d, i);
	f.inside && (c = c.slice(0, -1) + og(d));
	let p = o.move(i);
	return a(), n.attentionEncodeSurroundingInfo = {
		after: f.outside,
		before: u.outside
	}, s + c + p;
}
function lg(e, t, n) {
	return n.options.emphasis || "*";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/format-heading-as-setext.js
function ug(e, t) {
	let n = !1;
	return Zp(e, function(e) {
		if ("value" in e && /\r?\n|\r/.test(e.value) || e.type === "break") return n = !0, !1;
	}), !!((!e.depth || e.depth < 3) && tu(e) && (t.options.setext || n));
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/heading.js
function dg(e, t, n, r) {
	let i = Math.max(Math.min(6, e.depth || 1), 1), a = n.createTracker(r);
	if (ug(e, n)) {
		let t = n.enter("headingSetext"), r = n.enter("phrasing"), o = n.containerPhrasing(e, {
			...a.current(),
			before: "\n",
			after: "\n"
		});
		return r(), t(), o + "\n" + (i === 1 ? "=" : "-").repeat(o.length - (Math.max(o.lastIndexOf("\r"), o.lastIndexOf("\n")) + 1));
	}
	let o = "#".repeat(i), s = n.enter("headingAtx"), c = n.enter("phrasing");
	a.move(o + " ");
	let l = n.containerPhrasing(e, {
		before: "# ",
		after: "\n",
		...a.current()
	});
	return /^[\t ]/.test(l) && (l = og(l.charCodeAt(0)) + l.slice(1)), l = l ? o + " " + l : o, n.options.closeAtx && (l += " " + o), c(), s(), l;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/html.js
fg.peek = pg;
function fg(e) {
	return e.value || "";
}
function pg() {
	return "<";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/image.js
mg.peek = hg;
function mg(e, t, n, r) {
	let i = rg(n), a = i === "\"" ? "Quote" : "Apostrophe", o = n.enter("image"), s = n.enter("label"), c = n.createTracker(r), l = c.move("![");
	return l += c.move(n.safe(e.alt, {
		before: l,
		after: "]",
		...c.current()
	})), l += c.move("]("), s(), !e.url && e.title || /[\0- \u007F]/.test(e.url) ? (s = n.enter("destinationLiteral"), l += c.move("<"), l += c.move(n.safe(e.url, {
		before: l,
		after: ">",
		...c.current()
	})), l += c.move(">")) : (s = n.enter("destinationRaw"), l += c.move(n.safe(e.url, {
		before: l,
		after: e.title ? " " : ")",
		...c.current()
	}))), s(), e.title && (s = n.enter(`title${a}`), l += c.move(" " + i), l += c.move(n.safe(e.title, {
		before: l,
		after: i,
		...c.current()
	})), l += c.move(i), s()), l += c.move(")"), o(), l;
}
function hg() {
	return "!";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/image-reference.js
gg.peek = _g;
function gg(e, t, n, r) {
	let i = e.referenceType, a = n.enter("imageReference"), o = n.enter("label"), s = n.createTracker(r), c = s.move("!["), l = n.safe(e.alt, {
		before: c,
		after: "]",
		...s.current()
	});
	c += s.move(l + "]["), o();
	let u = n.stack;
	n.stack = [], o = n.enter("reference");
	let d = n.safe(n.associationId(e), {
		before: c,
		after: "]",
		...s.current()
	});
	return o(), n.stack = u, a(), i === "full" || !l || l !== d ? c += s.move(d + "]") : i === "shortcut" ? c = c.slice(0, -1) : c += s.move("]"), c;
}
function _g() {
	return "!";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/inline-code.js
vg.peek = yg;
function vg(e, t, n) {
	let r = e.value || "", i = "`", a = -1;
	for (; RegExp("(^|[^`])" + i + "([^`]|$)").test(r);) i += "`";
	for (/[^ \r\n]/.test(r) && (/^[ \r\n]/.test(r) && /[ \r\n]$/.test(r) || /^`|`$/.test(r)) && (r = " " + r + " "); ++a < n.unsafe.length;) {
		let e = n.unsafe[a], t = n.compilePattern(e), i;
		if (e.atBreak) for (; i = t.exec(r);) {
			let e = i.index;
			r.charCodeAt(e) === 10 && r.charCodeAt(e - 1) === 13 && e--, r = r.slice(0, e) + " " + r.slice(i.index + 1);
		}
	}
	return i + r + i;
}
function yg() {
	return "`";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/format-link-as-autolink.js
function bg(e, t) {
	let n = tu(e);
	return !!(!t.options.resourceLink && e.url && !e.title && e.children && e.children.length === 1 && e.children[0].type === "text" && (n === e.url || "mailto:" + n === e.url) && /^[a-z][a-z+.-]+:/i.test(e.url) && !/[\0- <>\u007F]/.test(e.url));
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/link.js
xg.peek = Sg;
function xg(e, t, n, r) {
	let i = rg(n), a = i === "\"" ? "Quote" : "Apostrophe", o = n.createTracker(r), s, c;
	if (bg(e, n)) {
		let t = n.stack;
		n.stack = [], s = n.enter("autolink");
		let r = o.move("<");
		return r += o.move(n.containerPhrasing(e, {
			before: r,
			after: ">",
			...o.current()
		})), r += o.move(">"), s(), n.stack = t, r;
	}
	s = n.enter("link"), c = n.enter("label");
	let l = o.move("[");
	return l += o.move(n.containerPhrasing(e, {
		before: l,
		after: "](",
		...o.current()
	})), l += o.move("]("), c(), !e.url && e.title || /[\0- \u007F]/.test(e.url) ? (c = n.enter("destinationLiteral"), l += o.move("<"), l += o.move(n.safe(e.url, {
		before: l,
		after: ">",
		...o.current()
	})), l += o.move(">")) : (c = n.enter("destinationRaw"), l += o.move(n.safe(e.url, {
		before: l,
		after: e.title ? " " : ")",
		...o.current()
	}))), c(), e.title && (c = n.enter(`title${a}`), l += o.move(" " + i), l += o.move(n.safe(e.title, {
		before: l,
		after: i,
		...o.current()
	})), l += o.move(i), c()), l += o.move(")"), s(), l;
}
function Sg(e, t, n) {
	return bg(e, n) ? "<" : "[";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/link-reference.js
Cg.peek = wg;
function Cg(e, t, n, r) {
	let i = e.referenceType, a = n.enter("linkReference"), o = n.enter("label"), s = n.createTracker(r), c = s.move("["), l = n.containerPhrasing(e, {
		before: c,
		after: "]",
		...s.current()
	});
	c += s.move(l + "]["), o();
	let u = n.stack;
	n.stack = [], o = n.enter("reference");
	let d = n.safe(n.associationId(e), {
		before: c,
		after: "]",
		...s.current()
	});
	return o(), n.stack = u, a(), i === "full" || !l || l !== d ? c += s.move(d + "]") : i === "shortcut" ? c = c.slice(0, -1) : c += s.move("]"), c;
}
function wg() {
	return "[";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-bullet.js
function Tg(e) {
	let t = e.options.bullet || "*";
	if (t !== "*" && t !== "+" && t !== "-") throw Error("Cannot serialize items with `" + t + "` for `options.bullet`, expected `*`, `+`, or `-`");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-bullet-other.js
function Eg(e) {
	let t = Tg(e), n = e.options.bulletOther;
	if (!n) return t === "*" ? "-" : "*";
	if (n !== "*" && n !== "+" && n !== "-") throw Error("Cannot serialize items with `" + n + "` for `options.bulletOther`, expected `*`, `+`, or `-`");
	if (n === t) throw Error("Expected `bullet` (`" + t + "`) and `bulletOther` (`" + n + "`) to be different");
	return n;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-bullet-ordered.js
function Dg(e) {
	let t = e.options.bulletOrdered || ".";
	if (t !== "." && t !== ")") throw Error("Cannot serialize items with `" + t + "` for `options.bulletOrdered`, expected `.` or `)`");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-rule.js
function Og(e) {
	let t = e.options.rule || "*";
	if (t !== "*" && t !== "-" && t !== "_") throw Error("Cannot serialize rules with `" + t + "` for `options.rule`, expected `*`, `-`, or `_`");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/list.js
function kg(e, t, n, r) {
	let i = n.enter("list"), a = n.bulletCurrent, o = e.ordered ? Dg(n) : Tg(n), s = e.ordered ? o === "." ? ")" : "." : Eg(n), c = t && n.bulletLastUsed ? o === n.bulletLastUsed : !1;
	if (!e.ordered) {
		let t = e.children ? e.children[0] : void 0;
		if ((o === "*" || o === "-") && t && (!t.children || !t.children[0]) && n.stack[n.stack.length - 1] === "list" && n.stack[n.stack.length - 2] === "listItem" && n.stack[n.stack.length - 3] === "list" && n.stack[n.stack.length - 4] === "listItem" && n.indexStack[n.indexStack.length - 1] === 0 && n.indexStack[n.indexStack.length - 2] === 0 && n.indexStack[n.indexStack.length - 3] === 0 && (c = !0), Og(n) === o && t) {
			let t = -1;
			for (; ++t < e.children.length;) {
				let n = e.children[t];
				if (n && n.type === "listItem" && n.children && n.children[0] && n.children[0].type === "thematicBreak") {
					c = !0;
					break;
				}
			}
		}
	}
	c && (o = s), n.bulletCurrent = o;
	let l = n.containerFlow(e, r);
	return n.bulletLastUsed = o, n.bulletCurrent = a, i(), l;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-list-item-indent.js
function Ag(e) {
	let t = e.options.listItemIndent || "one";
	if (t !== "tab" && t !== "one" && t !== "mixed") throw Error("Cannot serialize items with `" + t + "` for `options.listItemIndent`, expected `tab`, `one`, or `mixed`");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/list-item.js
function jg(e, t, n, r) {
	let i = Ag(n), a = n.bulletCurrent || Tg(n);
	t && t.type === "list" && t.ordered && (a = (typeof t.start == "number" && t.start > -1 ? t.start : 1) + (n.options.incrementListMarker === !1 ? 0 : t.children.indexOf(e)) + a);
	let o = a.length + 1;
	(i === "tab" || i === "mixed" && (t && t.type === "list" && t.spread || e.spread)) && (o = Math.ceil(o / 4) * 4);
	let s = n.createTracker(r);
	s.move(a + " ".repeat(o - a.length)), s.shift(o);
	let c = n.enter("listItem"), l = n.indentLines(n.containerFlow(e, s.current()), u);
	return c(), l;
	function u(e, t, n) {
		return t ? (n ? "" : " ".repeat(o)) + e : (n ? a : a + " ".repeat(o - a.length)) + e;
	}
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/paragraph.js
function Mg(e, t, n, r) {
	let i = n.enter("paragraph"), a = n.enter("phrasing"), o = n.containerPhrasing(e, r);
	return a(), i(), o;
}
//#endregion
//#region frontend/node_modules/mdast-util-phrasing/lib/index.js
var Ng = rt([
	"break",
	"delete",
	"emphasis",
	"footnote",
	"footnoteReference",
	"image",
	"imageReference",
	"inlineCode",
	"inlineMath",
	"link",
	"linkReference",
	"mdxJsxTextElement",
	"mdxTextExpression",
	"strong",
	"text",
	"textDirective"
]);
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/root.js
function Pg(e, t, n, r) {
	return (e.children.some(function(e) {
		return Ng(e);
	}) ? n.containerPhrasing : n.containerFlow).call(n, e, r);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-strong.js
function Fg(e) {
	let t = e.options.strong || "*";
	if (t !== "*" && t !== "_") throw Error("Cannot serialize strong with `" + t + "` for `options.strong`, expected `*`, or `_`");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/strong.js
Ig.peek = Lg;
function Ig(e, t, n, r) {
	let i = Fg(n), a = n.enter("strong"), o = n.createTracker(r), s = o.move(i + i), c = o.move(n.containerPhrasing(e, {
		after: i,
		before: s,
		...o.current()
	})), l = c.charCodeAt(0), u = sg(r.before.charCodeAt(r.before.length - 1), l, i);
	u.inside && (c = og(l) + c.slice(1));
	let d = c.charCodeAt(c.length - 1), f = sg(r.after.charCodeAt(0), d, i);
	f.inside && (c = c.slice(0, -1) + og(d));
	let p = o.move(i + i);
	return a(), n.attentionEncodeSurroundingInfo = {
		after: f.outside,
		before: u.outside
	}, s + c + p;
}
function Lg(e, t, n) {
	return n.options.strong || "*";
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/text.js
function Rg(e, t, n, r) {
	return n.safe(e.value, r);
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/util/check-rule-repetition.js
function zg(e) {
	let t = e.options.ruleRepetition || 3;
	if (t < 3) throw Error("Cannot serialize rules with repetition `" + t + "` for `options.ruleRepetition`, expected `3` or more");
	return t;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/thematic-break.js
function Bg(e, t, n) {
	let r = (Og(n) + (n.options.ruleSpaces ? " " : "")).repeat(zg(n));
	return n.options.ruleSpaces ? r.slice(0, -1) : r;
}
//#endregion
//#region frontend/node_modules/mdast-util-to-markdown/lib/handle/index.js
var Vg = {
	blockquote: qh,
	break: Zh,
	code: tg,
	definition: ig,
	emphasis: cg,
	hardBreak: Zh,
	heading: dg,
	html: fg,
	image: mg,
	imageReference: gg,
	inlineCode: vg,
	link: xg,
	linkReference: Cg,
	list: kg,
	listItem: jg,
	paragraph: Mg,
	root: Pg,
	strong: Ig,
	text: Rg,
	thematicBreak: Bg
};
//#endregion
//#region frontend/node_modules/mdast-util-gfm-table/lib/index.js
function Hg() {
	return {
		enter: {
			table: Ug,
			tableData: qg,
			tableHeader: qg,
			tableRow: Gg
		},
		exit: {
			codeText: Jg,
			table: Wg,
			tableData: Kg,
			tableHeader: Kg,
			tableRow: Kg
		}
	};
}
function Ug(e) {
	let t = e._align;
	this.enter({
		type: "table",
		align: t.map(function(e) {
			return e === "none" ? null : e;
		}),
		children: []
	}, e), this.data.inTable = !0;
}
function Wg(e) {
	this.exit(e), this.data.inTable = void 0;
}
function Gg(e) {
	this.enter({
		type: "tableRow",
		children: []
	}, e);
}
function Kg(e) {
	this.exit(e);
}
function qg(e) {
	this.enter({
		type: "tableCell",
		children: []
	}, e);
}
function Jg(e) {
	let t = this.resume();
	this.data.inTable && (t = t.replace(/\\([\\|])/g, Yg));
	let n = this.stack[this.stack.length - 1];
	n.type, n.value = t, this.exit(e);
}
function Yg(e, t) {
	return t === "|" ? t : e;
}
function Xg(e) {
	let t = e || {}, n = t.tableCellPadding, r = t.tablePipeAlign, i = t.stringLength, a = n ? " " : "|";
	return {
		unsafe: [
			{
				character: "\r",
				inConstruct: "tableCell"
			},
			{
				character: "\n",
				inConstruct: "tableCell"
			},
			{
				atBreak: !0,
				character: "|",
				after: "[	 :-]"
			},
			{
				character: "|",
				inConstruct: "tableCell"
			},
			{
				atBreak: !0,
				character: ":",
				after: "-"
			},
			{
				atBreak: !0,
				character: "-",
				after: "[:|-]"
			}
		],
		handlers: {
			inlineCode: f,
			table: o,
			tableCell: c,
			tableRow: s
		}
	};
	function o(e, t, n, r) {
		return l(u(e, n, r), e.align);
	}
	function s(e, t, n, r) {
		let i = l([d(e, n, r)]);
		return i.slice(0, i.indexOf("\n"));
	}
	function c(e, t, n, r) {
		let i = n.enter("tableCell"), o = n.enter("phrasing"), s = n.containerPhrasing(e, {
			...r,
			before: a,
			after: a
		});
		return o(), i(), s;
	}
	function l(e, t) {
		return Wh(e, {
			align: t,
			alignDelimiters: r,
			padding: n,
			stringLength: i
		});
	}
	function u(e, t, n) {
		let r = e.children, i = -1, a = [], o = t.enter("table");
		for (; ++i < r.length;) a[i] = d(r[i], t, n);
		return o(), a;
	}
	function d(e, t, n) {
		let r = e.children, i = -1, a = [], o = t.enter("tableRow");
		for (; ++i < r.length;) a[i] = c(r[i], e, t, n);
		return o(), a;
	}
	function f(e, t, n) {
		let r = Vg.inlineCode(e, t, n);
		return n.stack.includes("tableCell") && (r = r.replace(/\|/g, "\\$&")), r;
	}
}
//#endregion
//#region frontend/node_modules/mdast-util-gfm-task-list-item/lib/index.js
function Zg() {
	return { exit: {
		taskListCheckValueChecked: $g,
		taskListCheckValueUnchecked: $g,
		paragraph: e_
	} };
}
function Qg() {
	return {
		unsafe: [{
			atBreak: !0,
			character: "-",
			after: "[:|-]"
		}],
		handlers: { listItem: t_ }
	};
}
function $g(e) {
	let t = this.stack[this.stack.length - 2];
	t.type, t.checked = e.type === "taskListCheckValueChecked";
}
function e_(e) {
	let t = this.stack[this.stack.length - 2];
	if (t && t.type === "listItem" && typeof t.checked == "boolean") {
		let e = this.stack[this.stack.length - 1];
		e.type;
		let n = e.children[0];
		if (n && n.type === "text") {
			let r = t.children, i = -1, a;
			for (; ++i < r.length;) {
				let e = r[i];
				if (e.type === "paragraph") {
					a = e;
					break;
				}
			}
			a === e && (n.value = n.value.slice(1), n.value.length === 0 ? e.children.shift() : e.position && n.position && typeof n.position.start.offset == "number" && (n.position.start.column++, n.position.start.offset++, e.position.start = Object.assign({}, n.position.start)));
		}
	}
	this.exit(e);
}
function t_(e, t, n, r) {
	let i = e.children[0], a = typeof e.checked == "boolean" && i && i.type === "paragraph", o = "[" + (e.checked ? "x" : " ") + "] ", s = n.createTracker(r);
	a && s.move(o);
	let c = Vg.listItem(e, t, n, {
		...r,
		...s.current()
	});
	return a && (c = c.replace(/^(?:[*+-]|\d+\.)([\r\n]| {1,3})/, l)), c;
	function l(e) {
		return e + o;
	}
}
//#endregion
//#region frontend/node_modules/mdast-util-gfm/lib/index.js
function n_() {
	return [
		ch(),
		Mh(),
		Lh(),
		Hg(),
		Zg()
	];
}
function r_(e) {
	return { extensions: [
		lh(),
		Nh(e),
		Rh(),
		Xg(e),
		Qg()
	] };
}
//#endregion
//#region frontend/node_modules/micromark-extension-gfm-autolink-literal/lib/syntax.js
var i_ = {
	tokenize: v_,
	partial: !0
}, a_ = {
	tokenize: y_,
	partial: !0
}, o_ = {
	tokenize: b_,
	partial: !0
}, s_ = {
	tokenize: x_,
	partial: !0
}, c_ = {
	tokenize: S_,
	partial: !0
}, l_ = {
	name: "wwwAutolink",
	tokenize: g_,
	previous: C_
}, u_ = {
	name: "protocolAutolink",
	tokenize: __,
	previous: w_
}, d_ = {
	name: "emailAutolink",
	tokenize: h_,
	previous: T_
}, f_ = {};
function p_() {
	return { text: f_ };
}
for (var m_ = 48; m_ < 123;) f_[m_] = d_, m_++, m_ === 58 ? m_ = 65 : m_ === 91 && (m_ = 97);
f_[43] = d_, f_[45] = d_, f_[46] = d_, f_[95] = d_, f_[72] = [d_, u_], f_[104] = [d_, u_], f_[87] = [d_, l_], f_[119] = [d_, l_];
function h_(e, t, n) {
	let r = this, i, a;
	return o;
	function o(t) {
		return !E_(t) || !T_.call(r, r.previous) || D_(r.events) ? n(t) : (e.enter("literalAutolink"), e.enter("literalAutolinkEmail"), s(t));
	}
	function s(t) {
		return E_(t) ? (e.consume(t), s) : t === 64 ? (e.consume(t), c) : n(t);
	}
	function c(t) {
		return t === 46 ? e.check(c_, u, l)(t) : t === 45 || t === 95 || gu(t) ? (a = !0, e.consume(t), c) : u(t);
	}
	function l(t) {
		return e.consume(t), i = !0, c;
	}
	function u(o) {
		return a && i && hu(r.previous) ? (e.exit("literalAutolinkEmail"), e.exit("literalAutolink"), t(o)) : n(o);
	}
}
function g_(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return t !== 87 && t !== 119 || !C_.call(r, r.previous) || D_(r.events) ? n(t) : (e.enter("literalAutolink"), e.enter("literalAutolinkWww"), e.check(i_, e.attempt(a_, e.attempt(o_, a), n), n)(t));
	}
	function a(n) {
		return e.exit("literalAutolinkWww"), e.exit("literalAutolink"), t(n);
	}
}
function __(e, t, n) {
	let r = this, i = "", a = !1;
	return o;
	function o(t) {
		return (t === 72 || t === 104) && w_.call(r, r.previous) && !D_(r.events) ? (e.enter("literalAutolink"), e.enter("literalAutolinkHttp"), i += String.fromCodePoint(t), e.consume(t), s) : n(t);
	}
	function s(t) {
		if (hu(t) && i.length < 5) return i += String.fromCodePoint(t), e.consume(t), s;
		if (t === 58) {
			let n = i.toLowerCase();
			if (n === "http" || n === "https") return e.consume(t), c;
		}
		return n(t);
	}
	function c(t) {
		return t === 47 ? (e.consume(t), a ? l : (a = !0, c)) : n(t);
	}
	function l(t) {
		return t === null || vu(t) || Su(t) || Tu(t) || wu(t) ? n(t) : e.attempt(a_, e.attempt(o_, u), n)(t);
	}
	function u(n) {
		return e.exit("literalAutolinkHttp"), e.exit("literalAutolink"), t(n);
	}
}
function v_(e, t, n) {
	let r = 0;
	return i;
	function i(t) {
		return (t === 87 || t === 119) && r < 3 ? (r++, e.consume(t), i) : t === 46 && r === 3 ? (e.consume(t), a) : n(t);
	}
	function a(e) {
		return e === null ? n(e) : t(e);
	}
}
function y_(e, t, n) {
	let r, i, a;
	return o;
	function o(t) {
		return t === 46 || t === 95 ? e.check(s_, c, s)(t) : t === null || Su(t) || Tu(t) || t !== 45 && wu(t) ? c(t) : (a = !0, e.consume(t), o);
	}
	function s(t) {
		return t === 95 ? r = !0 : (i = r, r = void 0), e.consume(t), o;
	}
	function c(e) {
		return i || r || !a ? n(e) : t(e);
	}
}
function b_(e, t) {
	let n = 0, r = 0;
	return i;
	function i(o) {
		return o === 40 ? (n++, e.consume(o), i) : o === 41 && r < n ? a(o) : o === 33 || o === 34 || o === 38 || o === 39 || o === 41 || o === 42 || o === 44 || o === 46 || o === 58 || o === 59 || o === 60 || o === 63 || o === 93 || o === 95 || o === 126 ? e.check(s_, t, a)(o) : o === null || Su(o) || Tu(o) ? t(o) : (e.consume(o), i);
	}
	function a(t) {
		return t === 41 && r++, e.consume(t), i;
	}
}
function x_(e, t, n) {
	return r;
	function r(o) {
		return o === 33 || o === 34 || o === 39 || o === 41 || o === 42 || o === 44 || o === 46 || o === 58 || o === 59 || o === 63 || o === 95 || o === 126 ? (e.consume(o), r) : o === 38 ? (e.consume(o), a) : o === 93 ? (e.consume(o), i) : o === 60 || o === null || Su(o) || Tu(o) ? t(o) : n(o);
	}
	function i(e) {
		return e === null || e === 40 || e === 91 || Su(e) || Tu(e) ? t(e) : r(e);
	}
	function a(e) {
		return hu(e) ? o(e) : n(e);
	}
	function o(t) {
		return t === 59 ? (e.consume(t), r) : hu(t) ? (e.consume(t), o) : n(t);
	}
}
function S_(e, t, n) {
	return r;
	function r(t) {
		return e.consume(t), i;
	}
	function i(e) {
		return gu(e) ? n(e) : t(e);
	}
}
function C_(e) {
	return e === null || e === 40 || e === 42 || e === 95 || e === 91 || e === 93 || e === 126 || Su(e);
}
function w_(e) {
	return !hu(e);
}
function T_(e) {
	return !(e === 47 || E_(e));
}
function E_(e) {
	return e === 43 || e === 45 || e === 46 || e === 95 || gu(e);
}
function D_(e) {
	let t = e.length, n = !1;
	for (; t--;) {
		let r = e[t][1];
		if ((r.type === "labelLink" || r.type === "labelImage") && !r._balanced) {
			n = !0;
			break;
		}
		if (r._gfmAutolinkLiteralWalkedInto) {
			n = !1;
			break;
		}
	}
	return e.length > 0 && !n && (e[e.length - 1][1]._gfmAutolinkLiteralWalkedInto = !0), n;
}
//#endregion
//#region frontend/node_modules/micromark-extension-gfm-footnote/lib/syntax.js
var O_ = {
	tokenize: I_,
	partial: !0
};
function k_() {
	return {
		document: { 91: {
			name: "gfmFootnoteDefinition",
			tokenize: N_,
			continuation: { tokenize: P_ },
			exit: F_
		} },
		text: {
			91: {
				name: "gfmFootnoteCall",
				tokenize: M_
			},
			93: {
				name: "gfmPotentialFootnoteCall",
				add: "after",
				tokenize: A_,
				resolveTo: j_
			}
		}
	};
}
function A_(e, t, n) {
	let r = this, i = r.events.length, a = r.parser.gfmFootnotes || (r.parser.gfmFootnotes = []), o;
	for (; i--;) {
		let e = r.events[i][1];
		if (e.type === "labelImage") {
			o = e;
			break;
		}
		if (e.type === "gfmFootnoteCall" || e.type === "labelLink" || e.type === "label" || e.type === "image" || e.type === "link") break;
	}
	return s;
	function s(i) {
		if (!o || !o._balanced) return n(i);
		let s = mu(r.sliceSerialize({
			start: o.end,
			end: r.now()
		}));
		return s.codePointAt(0) !== 94 || !a.includes(s.slice(1)) ? n(i) : (e.enter("gfmFootnoteCallLabelMarker"), e.consume(i), e.exit("gfmFootnoteCallLabelMarker"), t(i));
	}
}
function j_(e, t) {
	let n = e.length;
	for (; n--;) if (e[n][1].type === "labelImage" && e[n][0] === "enter") {
		e[n][1];
		break;
	}
	e[n + 1][1].type = "data", e[n + 3][1].type = "gfmFootnoteCallLabelMarker";
	let r = {
		type: "gfmFootnoteCall",
		start: Object.assign({}, e[n + 3][1].start),
		end: Object.assign({}, e[e.length - 1][1].end)
	}, i = {
		type: "gfmFootnoteCallMarker",
		start: Object.assign({}, e[n + 3][1].end),
		end: Object.assign({}, e[n + 3][1].end)
	};
	i.end.column++, i.end.offset++, i.end._bufferIndex++;
	let a = {
		type: "gfmFootnoteCallString",
		start: Object.assign({}, i.end),
		end: Object.assign({}, e[e.length - 1][1].start)
	}, o = {
		type: "chunkString",
		contentType: "string",
		start: Object.assign({}, a.start),
		end: Object.assign({}, a.end)
	}, s = [
		e[n + 1],
		e[n + 2],
		[
			"enter",
			r,
			t
		],
		e[n + 3],
		e[n + 4],
		[
			"enter",
			i,
			t
		],
		[
			"exit",
			i,
			t
		],
		[
			"enter",
			a,
			t
		],
		[
			"enter",
			o,
			t
		],
		[
			"exit",
			o,
			t
		],
		[
			"exit",
			a,
			t
		],
		e[e.length - 2],
		e[e.length - 1],
		[
			"exit",
			r,
			t
		]
	];
	return e.splice(n, e.length - n + 1, ...s), e;
}
function M_(e, t, n) {
	let r = this, i = r.parser.gfmFootnotes || (r.parser.gfmFootnotes = []), a = 0, o;
	return s;
	function s(t) {
		return e.enter("gfmFootnoteCall"), e.enter("gfmFootnoteCallLabelMarker"), e.consume(t), e.exit("gfmFootnoteCallLabelMarker"), c;
	}
	function c(t) {
		return t === 94 ? (e.enter("gfmFootnoteCallMarker"), e.consume(t), e.exit("gfmFootnoteCallMarker"), e.enter("gfmFootnoteCallString"), e.enter("chunkString").contentType = "string", l) : n(t);
	}
	function l(s) {
		if (a > 999 || s === 93 && !o || s === null || s === 91 || Su(s)) return n(s);
		if (s === 93) {
			e.exit("chunkString");
			let a = e.exit("gfmFootnoteCallString");
			return i.includes(mu(r.sliceSerialize(a))) ? (e.enter("gfmFootnoteCallLabelMarker"), e.consume(s), e.exit("gfmFootnoteCallLabelMarker"), e.exit("gfmFootnoteCall"), t) : n(s);
		}
		return Su(s) || (o = !0), a++, e.consume(s), s === 92 ? u : l;
	}
	function u(t) {
		return t === 91 || t === 92 || t === 93 ? (e.consume(t), a++, l) : l(t);
	}
}
function N_(e, t, n) {
	let r = this, i = r.parser.gfmFootnotes || (r.parser.gfmFootnotes = []), a, o = 0, s;
	return c;
	function c(t) {
		return e.enter("gfmFootnoteDefinition")._container = !0, e.enter("gfmFootnoteDefinitionLabel"), e.enter("gfmFootnoteDefinitionLabelMarker"), e.consume(t), e.exit("gfmFootnoteDefinitionLabelMarker"), l;
	}
	function l(t) {
		return t === 94 ? (e.enter("gfmFootnoteDefinitionMarker"), e.consume(t), e.exit("gfmFootnoteDefinitionMarker"), e.enter("gfmFootnoteDefinitionLabelString"), e.enter("chunkString").contentType = "string", u) : n(t);
	}
	function u(t) {
		if (o > 999 || t === 93 && !s || t === null || t === 91 || Su(t)) return n(t);
		if (t === 93) {
			e.exit("chunkString");
			let n = e.exit("gfmFootnoteDefinitionLabelString");
			return a = mu(r.sliceSerialize(n)), e.enter("gfmFootnoteDefinitionLabelMarker"), e.consume(t), e.exit("gfmFootnoteDefinitionLabelMarker"), e.exit("gfmFootnoteDefinitionLabel"), f;
		}
		return Su(t) || (s = !0), o++, e.consume(t), t === 92 ? d : u;
	}
	function d(t) {
		return t === 91 || t === 92 || t === 93 ? (e.consume(t), o++, u) : u(t);
	}
	function f(t) {
		return t === 58 ? (e.enter("definitionMarker"), e.consume(t), e.exit("definitionMarker"), i.includes(a) || i.push(a), Ou(e, p, "gfmFootnoteDefinitionWhitespace")) : n(t);
	}
	function p(e) {
		return t(e);
	}
}
function P_(e, t, n) {
	return e.check(Uu, t, e.attempt(O_, t, n));
}
function F_(e) {
	e.exit("gfmFootnoteDefinition");
}
function I_(e, t, n) {
	let r = this;
	return Ou(e, i, "gfmFootnoteDefinitionIndent", 5);
	function i(e) {
		let i = r.events[r.events.length - 1];
		return i && i[1].type === "gfmFootnoteDefinitionIndent" && i[2].sliceSerialize(i[1], !0).length === 4 ? t(e) : n(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-extension-gfm-strikethrough/lib/syntax.js
function L_(e) {
	let t = (e || {}).singleTilde, n = {
		name: "strikethrough",
		tokenize: i,
		resolveAll: r
	};
	return t ??= !0, {
		text: { 126: n },
		insideSpan: { null: [n] },
		attentionMarkers: { null: [126] }
	};
	function r(e, t) {
		let n = -1;
		for (; ++n < e.length;) if (e[n][0] === "enter" && e[n][1].type === "strikethroughSequenceTemporary" && e[n][1]._close) {
			let r = n;
			for (; r--;) if (e[r][0] === "exit" && e[r][1].type === "strikethroughSequenceTemporary" && e[r][1]._open && e[n][1].end.offset - e[n][1].start.offset === e[r][1].end.offset - e[r][1].start.offset) {
				e[n][1].type = "strikethroughSequence", e[r][1].type = "strikethroughSequence";
				let i = {
					type: "strikethrough",
					start: Object.assign({}, e[r][1].start),
					end: Object.assign({}, e[n][1].end)
				}, a = {
					type: "strikethroughText",
					start: Object.assign({}, e[r][1].end),
					end: Object.assign({}, e[n][1].start)
				}, o = [
					[
						"enter",
						i,
						t
					],
					[
						"enter",
						e[r][1],
						t
					],
					[
						"exit",
						e[r][1],
						t
					],
					[
						"enter",
						a,
						t
					]
				], s = t.parser.constructs.insideSpan.null;
				s && su(o, o.length, 0, Iu(s, e.slice(r + 1, n), t)), su(o, o.length, 0, [
					[
						"exit",
						a,
						t
					],
					[
						"enter",
						e[n][1],
						t
					],
					[
						"exit",
						e[n][1],
						t
					],
					[
						"exit",
						i,
						t
					]
				]), su(e, r - 1, n - r + 3, o), n = r + o.length - 2;
				break;
			}
		}
		for (n = -1; ++n < e.length;) e[n][1].type === "strikethroughSequenceTemporary" && (e[n][1].type = "data");
		return e;
	}
	function i(e, n, r) {
		let i = this.previous, a = this.events, o = 0;
		return s;
		function s(t) {
			return i === 126 && a[a.length - 1][1].type !== "characterEscape" ? r(t) : (e.enter("strikethroughSequenceTemporary"), c(t));
		}
		function c(a) {
			let s = Fu(i);
			if (a === 126) return o > 1 ? r(a) : (e.consume(a), o++, c);
			if (o < 2 && !t) return r(a);
			let l = e.exit("strikethroughSequenceTemporary"), u = Fu(a);
			return l._open = !u || u === 2 && !!s, l._close = !s || s === 2 && !!u, n(a);
		}
	}
}
//#endregion
//#region frontend/node_modules/micromark-extension-gfm-table/lib/edit-map.js
var R_ = class {
	constructor() {
		this.map = [];
	}
	add(e, t, n) {
		z_(this, e, t, n);
	}
	consume(e) {
		/* c8 ignore next 3 -- `resolve` is never called without tables, so without edits. */
		if (this.map.sort(function(e, t) {
			return e[0] - t[0];
		}), this.map.length === 0) return;
		let t = this.map.length, n = [];
		for (; t > 0;) --t, n.push(e.slice(this.map[t][0] + this.map[t][1]), this.map[t][2]), e.length = this.map[t][0];
		n.push(e.slice()), e.length = 0;
		let r = n.pop();
		for (; r;) {
			for (let t of r) e.push(t);
			r = n.pop();
		}
		this.map.length = 0;
	}
};
function z_(e, t, n, r) {
	let i = 0;
	if (!(n === 0 && r.length === 0)) {
		for (; i < e.map.length;) {
			if (e.map[i][0] === t) {
				e.map[i][1] += n, e.map[i][2].push(...r);
				return;
			}
			i += 1;
		}
		e.map.push([
			t,
			n,
			r
		]);
	}
}
//#endregion
//#region frontend/node_modules/micromark-extension-gfm-table/lib/infer.js
function B_(e, t) {
	let n = !1, r = [];
	for (; t < e.length;) {
		let i = e[t];
		if (n) {
			if (i[0] === "enter") i[1].type === "tableContent" && r.push(e[t + 1][1].type === "tableDelimiterMarker" ? "left" : "none");
			else if (i[1].type === "tableContent") {
				if (e[t - 1][1].type === "tableDelimiterMarker") {
					let e = r.length - 1;
					r[e] = r[e] === "left" ? "center" : "right";
				}
			} else if (i[1].type === "tableDelimiterRow") break;
		} else i[0] === "enter" && i[1].type === "tableDelimiterRow" && (n = !0);
		t += 1;
	}
	return r;
}
//#endregion
//#region frontend/node_modules/micromark-extension-gfm-table/lib/syntax.js
function V_() {
	return { flow: { null: {
		name: "table",
		tokenize: H_,
		resolveAll: U_
	} } };
}
function H_(e, t, n) {
	let r = this, i = 0, a = 0, o;
	return s;
	function s(e) {
		let t = r.events.length - 1;
		for (; t > -1;) {
			let e = r.events[t][1].type;
			if (e === "lineEnding" || e === "linePrefix") t--;
			else break;
		}
		let i = t > -1 ? r.events[t][1].type : null, a = i === "tableHead" || i === "tableRow" ? S : c;
		return a === S && r.parser.lazy[r.now().line] ? n(e) : a(e);
	}
	function c(t) {
		return e.enter("tableHead"), e.enter("tableRow"), l(t);
	}
	function l(e) {
		return e === 124 ? u(e) : (o = !0, a += 1, u(e));
	}
	function u(t) {
		return t === null ? n(t) : $(t) ? a > 1 ? (a = 0, r.interrupt = !0, e.exit("tableRow"), e.enter("lineEnding"), e.consume(t), e.exit("lineEnding"), p) : n(t) : Cu(t) ? Ou(e, u, "whitespace")(t) : (a += 1, o && (o = !1, i += 1), t === 124 ? (e.enter("tableCellDivider"), e.consume(t), e.exit("tableCellDivider"), o = !0, u) : (e.enter("data"), d(t)));
	}
	function d(t) {
		return t === null || t === 124 || Su(t) ? (e.exit("data"), u(t)) : (e.consume(t), t === 92 ? f : d);
	}
	function f(t) {
		return t === 92 || t === 124 ? (e.consume(t), d) : d(t);
	}
	function p(t) {
		return r.interrupt = !1, r.parser.lazy[r.now().line] ? n(t) : (e.enter("tableDelimiterRow"), o = !1, Cu(t) ? Ou(e, m, "linePrefix", r.parser.constructs.disable.null.includes("codeIndented") ? void 0 : 4)(t) : m(t));
	}
	function m(t) {
		return t === 45 || t === 58 ? g(t) : t === 124 ? (o = !0, e.enter("tableCellDivider"), e.consume(t), e.exit("tableCellDivider"), h) : x(t);
	}
	function h(t) {
		return Cu(t) ? Ou(e, g, "whitespace")(t) : g(t);
	}
	function g(t) {
		return t === 58 ? (a += 1, o = !0, e.enter("tableDelimiterMarker"), e.consume(t), e.exit("tableDelimiterMarker"), _) : t === 45 ? (a += 1, _(t)) : t === null || $(t) ? b(t) : x(t);
	}
	function _(t) {
		return t === 45 ? (e.enter("tableDelimiterFiller"), v(t)) : x(t);
	}
	function v(t) {
		return t === 45 ? (e.consume(t), v) : t === 58 ? (o = !0, e.exit("tableDelimiterFiller"), e.enter("tableDelimiterMarker"), e.consume(t), e.exit("tableDelimiterMarker"), y) : (e.exit("tableDelimiterFiller"), y(t));
	}
	function y(t) {
		return Cu(t) ? Ou(e, b, "whitespace")(t) : b(t);
	}
	function b(n) {
		return n === 124 ? m(n) : n === null || $(n) ? !o || i !== a ? x(n) : (e.exit("tableDelimiterRow"), e.exit("tableHead"), t(n)) : x(n);
	}
	function x(e) {
		return n(e);
	}
	function S(t) {
		return e.enter("tableRow"), C(t);
	}
	function C(n) {
		return n === 124 ? (e.enter("tableCellDivider"), e.consume(n), e.exit("tableCellDivider"), C) : n === null || $(n) ? (e.exit("tableRow"), t(n)) : Cu(n) ? Ou(e, C, "whitespace")(n) : (e.enter("data"), w(n));
	}
	function w(t) {
		return t === null || t === 124 || Su(t) ? (e.exit("data"), C(t)) : (e.consume(t), t === 92 ? T : w);
	}
	function T(t) {
		return t === 92 || t === 124 ? (e.consume(t), w) : w(t);
	}
}
function U_(e, t) {
	let n = -1, r = !0, i = 0, a = [
		0,
		0,
		0,
		0
	], o = [
		0,
		0,
		0,
		0
	], s = !1, c = 0, l, u, d, f = new R_();
	for (; ++n < e.length;) {
		let p = e[n], m = p[1];
		p[0] === "enter" ? m.type === "tableHead" ? (s = !1, c !== 0 && (G_(f, t, c, l, u), u = void 0, c = 0), l = {
			type: "table",
			start: Object.assign({}, m.start),
			end: Object.assign({}, m.end)
		}, f.add(n, 0, [[
			"enter",
			l,
			t
		]])) : m.type === "tableRow" || m.type === "tableDelimiterRow" ? (r = !0, d = void 0, a = [
			0,
			0,
			0,
			0
		], o = [
			0,
			n + 1,
			0,
			0
		], s && (s = !1, u = {
			type: "tableBody",
			start: Object.assign({}, m.start),
			end: Object.assign({}, m.end)
		}, f.add(n, 0, [[
			"enter",
			u,
			t
		]])), i = m.type === "tableDelimiterRow" ? 2 : u ? 3 : 1) : i && (m.type === "data" || m.type === "tableDelimiterMarker" || m.type === "tableDelimiterFiller") ? (r = !1, o[2] === 0 && (a[1] !== 0 && (o[0] = o[1], d = W_(f, t, a, i, void 0, d), a = [
			0,
			0,
			0,
			0
		]), o[2] = n)) : m.type === "tableCellDivider" && (r ? r = !1 : (a[1] !== 0 && (o[0] = o[1], d = W_(f, t, a, i, void 0, d)), a = o, o = [
			a[1],
			n,
			0,
			0
		])) : m.type === "tableHead" ? (s = !0, c = n) : m.type === "tableRow" || m.type === "tableDelimiterRow" ? (c = n, a[1] === 0 ? o[1] !== 0 && (d = W_(f, t, o, i, n, d)) : (o[0] = o[1], d = W_(f, t, a, i, n, d)), i = 0) : i && (m.type === "data" || m.type === "tableDelimiterMarker" || m.type === "tableDelimiterFiller") && (o[3] = n);
	}
	for (c !== 0 && G_(f, t, c, l, u), f.consume(t.events), n = -1; ++n < t.events.length;) {
		let e = t.events[n];
		e[0] === "enter" && e[1].type === "table" && (e[1]._align = B_(t.events, n));
	}
	return e;
}
function W_(e, t, n, r, i, a) {
	let o = r === 1 ? "tableHeader" : r === 2 ? "tableDelimiter" : "tableData";
	n[0] !== 0 && (a.end = Object.assign({}, K_(t.events, n[0])), e.add(n[0], 0, [[
		"exit",
		a,
		t
	]]));
	let s = K_(t.events, n[1]);
	if (a = {
		type: o,
		start: Object.assign({}, s),
		end: Object.assign({}, s)
	}, e.add(n[1], 0, [[
		"enter",
		a,
		t
	]]), n[2] !== 0) {
		let i = K_(t.events, n[2]), a = K_(t.events, n[3]), o = {
			type: "tableContent",
			start: Object.assign({}, i),
			end: Object.assign({}, a)
		};
		if (e.add(n[2], 0, [[
			"enter",
			o,
			t
		]]), r !== 2) {
			let r = t.events[n[2]], i = t.events[n[3]];
			if (r[1].end = Object.assign({}, i[1].end), r[1].type = "chunkText", r[1].contentType = "text", n[3] > n[2] + 1) {
				let t = n[2] + 1, r = n[3] - n[2] - 1;
				e.add(t, r, []);
			}
		}
		e.add(n[3] + 1, 0, [[
			"exit",
			o,
			t
		]]);
	}
	return i !== void 0 && (a.end = Object.assign({}, K_(t.events, i)), e.add(i, 0, [[
		"exit",
		a,
		t
	]]), a = void 0), a;
}
function G_(e, t, n, r, i) {
	let a = [], o = K_(t.events, n);
	i && (i.end = Object.assign({}, o), a.push([
		"exit",
		i,
		t
	])), r.end = Object.assign({}, o), a.push([
		"exit",
		r,
		t
	]), e.add(n + 1, 0, a);
}
function K_(e, t) {
	let n = e[t], r = n[0] === "enter" ? "start" : "end";
	return n[1][r];
}
//#endregion
//#region frontend/node_modules/micromark-extension-gfm-task-list-item/lib/syntax.js
var q_ = {
	name: "tasklistCheck",
	tokenize: Y_
};
function J_() {
	return { text: { 91: q_ } };
}
function Y_(e, t, n) {
	let r = this;
	return i;
	function i(t) {
		return r.previous !== null || !r._gfmTasklistFirstContentOfListItem ? n(t) : (e.enter("taskListCheck"), e.enter("taskListCheckMarker"), e.consume(t), e.exit("taskListCheckMarker"), a);
	}
	function a(t) {
		return Su(t) ? (e.enter("taskListCheckValueUnchecked"), e.consume(t), e.exit("taskListCheckValueUnchecked"), o) : t === 88 || t === 120 ? (e.enter("taskListCheckValueChecked"), e.consume(t), e.exit("taskListCheckValueChecked"), o) : n(t);
	}
	function o(t) {
		return t === 93 ? (e.enter("taskListCheckMarker"), e.consume(t), e.exit("taskListCheckMarker"), e.exit("taskListCheck"), s) : n(t);
	}
	function s(r) {
		return $(r) ? t(r) : Cu(r) ? e.check({ tokenize: X_ }, t, n)(r) : n(r);
	}
}
function X_(e, t, n) {
	return Ou(e, r, "whitespace");
	function r(e) {
		return e === null ? n(e) : t(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-extension-gfm/index.js
function Z_(e) {
	return uu([
		p_(),
		k_(),
		L_(e),
		V_(),
		J_()
	]);
}
//#endregion
//#region frontend/node_modules/remark-gfm/lib/index.js
var Q_ = {};
function $_(e) {
	let t = this, n = e || Q_, r = t.data(), i = r.micromarkExtensions ||= [], a = r.fromMarkdownExtensions ||= [], o = r.toMarkdownExtensions ||= [];
	i.push(Z_(n)), a.push(n_()), o.push(r_(n));
}
//#endregion
//#region frontend/node_modules/mdast-util-math/lib/index.js
function ev() {
	return {
		enter: {
			mathFlow: e,
			mathFlowFenceMeta: t,
			mathText: a
		},
		exit: {
			mathFlow: i,
			mathFlowFence: r,
			mathFlowFenceMeta: n,
			mathFlowValue: s,
			mathText: o,
			mathTextData: s
		}
	};
	function e(e) {
		this.enter({
			type: "math",
			meta: null,
			value: "",
			data: {
				hName: "pre",
				hChildren: [{
					type: "element",
					tagName: "code",
					properties: { className: ["language-math", "math-display"] },
					children: []
				}]
			}
		}, e);
	}
	function t() {
		this.buffer();
	}
	function n() {
		let e = this.resume(), t = this.stack[this.stack.length - 1];
		t.type, t.meta = e;
	}
	function r() {
		this.data.mathFlowInside || (this.buffer(), this.data.mathFlowInside = !0);
	}
	function i(e) {
		let t = this.resume().replace(/^(\r?\n|\r)|(\r?\n|\r)$/g, ""), n = this.stack[this.stack.length - 1];
		n.type, this.exit(e), n.value = t;
		let r = n.data.hChildren[0];
		r.type, r.tagName, r.children.push({
			type: "text",
			value: t
		}), this.data.mathFlowInside = void 0;
	}
	function a(e) {
		this.enter({
			type: "inlineMath",
			value: "",
			data: {
				hName: "code",
				hProperties: { className: ["language-math", "math-inline"] },
				hChildren: []
			}
		}, e), this.buffer();
	}
	function o(e) {
		let t = this.resume(), n = this.stack[this.stack.length - 1];
		n.type, this.exit(e), n.value = t, n.data.hChildren.push({
			type: "text",
			value: t
		});
	}
	function s(e) {
		this.config.enter.data.call(this, e), this.config.exit.data.call(this, e);
	}
}
function tv(e) {
	let t = (e || {}).singleDollarTextMath;
	return t ??= !0, r.peek = i, {
		unsafe: [
			{
				character: "\r",
				inConstruct: "mathFlowMeta"
			},
			{
				character: "\n",
				inConstruct: "mathFlowMeta"
			},
			{
				character: "$",
				after: t ? void 0 : "\\$",
				inConstruct: "phrasing"
			},
			{
				character: "$",
				inConstruct: "mathFlowMeta"
			},
			{
				atBreak: !0,
				character: "$",
				after: "\\$"
			}
		],
		handlers: {
			math: n,
			inlineMath: r
		}
	};
	function n(e, t, n, r) {
		let i = e.value || "", a = n.createTracker(r), o = "$".repeat(Math.max(Qh(i, "$") + 1, 2)), s = n.enter("mathFlow"), c = a.move(o);
		if (e.meta) {
			let t = n.enter("mathFlowMeta");
			c += a.move(n.safe(e.meta, {
				after: "\n",
				before: c,
				encode: ["$"],
				...a.current()
			})), t();
		}
		return c += a.move("\n"), i && (c += a.move(i + "\n")), c += a.move(o), s(), c;
	}
	function r(e, n, r) {
		let i = e.value || "", a = 1;
		for (t || a++; RegExp("(^|[^$])" + "\\$".repeat(a) + "([^$]|$)").test(i);) a++;
		let o = "$".repeat(a);
		/[^ \r\n]/.test(i) && (/^[ \r\n]/.test(i) && /[ \r\n]$/.test(i) || /^\$|\$$/.test(i)) && (i = " " + i + " ");
		let s = -1;
		for (; ++s < r.unsafe.length;) {
			let e = r.unsafe[s];
			if (!e.atBreak) continue;
			let t = r.compilePattern(e), n;
			for (; n = t.exec(i);) {
				let e = n.index;
				i.codePointAt(e) === 10 && i.codePointAt(e - 1) === 13 && e--, i = i.slice(0, e) + " " + i.slice(n.index + 1);
			}
		}
		return o + i + o;
	}
	function i() {
		return "$";
	}
}
//#endregion
//#region frontend/node_modules/micromark-extension-math-extended/lib/math-flow.js
var nv = av(), rv = {
	tokenize: ov,
	concrete: !0,
	name: "mathFlow"
}, iv = {
	tokenize: sv,
	partial: !0
};
function av() {
	return {
		tokenize: e,
		concrete: !0,
		name: "mathFlow"
	};
	function e(e, t, n) {
		let r = this, i = r.events[r.events.length - 1], a = i && i[1].type === "linePrefix" ? i[2].sliceSerialize(i[1], !0).length : 0, o = 0;
		return s;
		function s(t) {
			return e.enter("mathFlow"), e.enter("mathFlowFence"), e.enter("mathFlowFenceSequence"), c(t);
		}
		function c(t) {
			return t === 36 ? (e.consume(t), o++, c) : o < 2 ? n(t) : (e.exit("mathFlowFenceSequence"), Ou(e, l, "whitespace")(t));
		}
		function l(t) {
			return t === null || $(t) ? d(t) : (e.enter("mathFlowFenceMeta"), e.enter("chunkString", { contentType: "string" }), u(t));
		}
		function u(t) {
			return t === null || $(t) ? (e.exit("chunkString"), e.exit("mathFlowFenceMeta"), d(t)) : t === 36 ? n(t) : (e.consume(t), u);
		}
		function d(n) {
			return e.exit("mathFlowFence"), r.interrupt ? t(n) : e.attempt(iv, f, _)(n);
		}
		function f(t) {
			return e.attempt({
				tokenize: g,
				partial: !0
			}, _, p)(t);
		}
		function p(t) {
			return (a ? Ou(e, m, "linePrefix", a + 1) : m)(t);
		}
		function m(t) {
			return t === null ? _(t) : $(t) ? e.attempt(iv, f, _)(t) : (e.enter("mathFlowValue"), h(t));
		}
		function h(t) {
			return t === null || $(t) ? (e.exit("mathFlowValue"), m(t)) : (e.consume(t), h);
		}
		function g(e, t, n) {
			let i = 0;
			return Ou(e, a, "linePrefix", r.parser.constructs.disable.null.includes("codeIndented") ? void 0 : 4);
			function a(t) {
				return e.enter("mathFlowFence"), e.enter("mathFlowFenceSequence"), s(t);
			}
			function s(t) {
				return t === 36 ? (i++, e.consume(t), s) : i < o ? n(t) : (e.exit("mathFlowFenceSequence"), Ou(e, c, "whitespace")(t));
			}
			function c(r) {
				return r === null || $(r) ? (e.exit("mathFlowFence"), t(r)) : n(r);
			}
		}
		function _(n) {
			return e.exit("mathFlow"), t(n);
		}
	}
}
function ov(e, t, n) {
	let r = this, i = r.events[r.events.length - 1], a = i && i[1].type === "linePrefix" ? i[2].sliceSerialize(i[1], !0).length : 0;
	return o;
	function o(t) {
		return e.enter("mathFlow"), e.enter("mathFlowFence"), e.enter("mathFlowFenceSequence"), e.consume(t), s;
	}
	function s(t) {
		return t === 91 ? (e.consume(t), e.exit("mathFlowFenceSequence"), e.exit("mathFlowFence"), c) : n(t);
	}
	function c(t) {
		return t === null ? n(t) : $(t) ? e.attempt(iv, l, n)(t) : t === 92 ? e.attempt({
			tokenize: p,
			partial: !0
		}, m, u)(t) : (e.enter("mathFlowValue"), f(t));
	}
	function l(t) {
		return (a ? Ou(e, c, "linePrefix", a + 1) : c)(t);
	}
	function u(t) {
		return e.enter("mathFlowValue"), e.consume(t), d;
	}
	function d(t) {
		return t === 92 ? (e.consume(t), f) : t === 91 ? n(t) : f(t);
	}
	function f(t) {
		return t === null || t === 92 || $(t) ? (e.exit("mathFlowValue"), c(t)) : (e.consume(t), f);
	}
	function p(e, t, n) {
		return r;
		function r(t) {
			return e.enter("mathFlowFence"), e.enter("mathFlowFenceSequence"), e.consume(t), i;
		}
		function i(t) {
			return t === 93 ? (e.consume(t), e.exit("mathFlowFenceSequence"), Ou(e, a, "whitespace")) : n(t);
		}
		function a(r) {
			return r === null || $(r) ? (e.exit("mathFlowFence"), t(r)) : n(r);
		}
	}
	function m(n) {
		return e.exit("mathFlow"), t(n);
	}
}
function sv(e, t, n) {
	let r = this;
	return i;
	function i(n) {
		return n === null ? t(n) : (e.enter("lineEnding"), e.consume(n), e.exit("lineEnding"), a);
	}
	function a(e) {
		return r.parser.lazy[r.now().line] ? n(e) : t(e);
	}
}
//#endregion
//#region frontend/node_modules/micromark-extension-math-extended/lib/math-text.js
function cv(e) {
	let t = (e || {}).singleDollarTextMath;
	return t ??= !0, {
		dollar: lv(t),
		backslash: uv(40, 41)
	};
}
function lv(e) {
	return {
		tokenize: t,
		resolve: dv,
		previous: fv,
		name: "mathText"
	};
	function t(t, n, r) {
		let i = 0, a, o;
		return s;
		function s(e) {
			return t.enter("mathText"), t.enter("mathTextSequence"), c(e);
		}
		function c(n) {
			return n === 36 ? (t.consume(n), i++, c) : i < 2 && !e ? r(n) : (t.exit("mathTextSequence"), l(n));
		}
		function l(e) {
			return e === null ? r(e) : e === 36 ? (o = t.enter("mathTextSequence"), a = 0, d(e)) : e === 32 ? (t.enter("space"), t.consume(e), t.exit("space"), l) : $(e) ? (t.enter("lineEnding"), t.consume(e), t.exit("lineEnding"), l) : (t.enter("mathTextData"), u(e));
		}
		function u(e) {
			return e === null || e === 32 || e === 36 || $(e) ? (t.exit("mathTextData"), l(e)) : (t.consume(e), u);
		}
		function d(e) {
			return e === 36 ? (t.consume(e), a++, d) : a === i ? (t.exit("mathTextSequence"), t.exit("mathText"), n(e)) : (o.type = "mathTextData", u(e));
		}
	}
}
function uv(e, t) {
	return {
		tokenize: n,
		resolve: dv,
		previous: pv,
		name: "mathText"
	};
	function n(n, r, i) {
		let a = 0, o, s;
		return c;
		function c(e) {
			return n.enter("mathText"), n.enter("mathTextSequence"), l(e);
		}
		function l(t) {
			return a === 0 && t === 92 ? (n.consume(t), a = 1, l) : a === 1 && t === e ? (n.consume(t), a = 2, l) : a < 2 ? i(t) : (n.exit("mathTextSequence"), u(t));
		}
		function u(e) {
			return e === null ? i(e) : e === 92 ? (s = n.enter("mathTextSequence"), o = 0, f(e)) : e === 32 ? (n.enter("space"), n.consume(e), n.exit("space"), u) : $(e) ? (n.enter("lineEnding"), n.consume(e), n.exit("lineEnding"), u) : (n.enter("mathTextData"), d(e));
		}
		function d(e) {
			return e === null || e === 32 || e === 92 || $(e) ? (n.exit("mathTextData"), u(e)) : (n.consume(e), d);
		}
		function f(e) {
			if (o === 2) return n.exit("mathTextSequence"), n.exit("mathText"), r(e);
			if (o === 0) {
				if (e === 92) return n.consume(e), o = 1, f;
			} else if (o === 1 && e === t) return n.consume(e), o = 2, f;
			return s.type = "mathTextData", d(e);
		}
	}
}
function dv(e) {
	let t = e.length - 4, n = 3, r, i;
	if ((e[n][1].type === "lineEnding" || e[n][1].type === "space") && (e[t][1].type === "lineEnding" || e[t][1].type === "space")) {
		for (r = n; ++r < t;) if (e[r][1].type === "mathTextData") {
			e[t][1].type = "mathTextPadding", e[n][1].type = "mathTextPadding", n += 2, t -= 2;
			break;
		}
	}
	for (r = n - 1, t++; ++r <= t;) i === void 0 ? r !== t && e[r][1].type !== "lineEnding" && (i = r) : (r === t || e[r][1].type === "lineEnding") && (e[i][1].type = "mathTextData", r !== i + 2 && (e[i][1].end = e[r - 1][1].end, e.splice(i + 2, r - i - 2), t -= r - i - 2, r = i + 2), i = void 0);
	return e;
}
function fv(e) {
	return e !== 36 || this.events[this.events.length - 1][1].type === "characterEscape";
}
function pv(e) {
	return e !== 92 || this.events[this.events.length - 1][1].type === "characterEscape";
}
//#endregion
//#region frontend/node_modules/micromark-extension-math-extended/lib/syntax.js
function mv(e) {
	let t = cv(e), n = e?.backslashDelimiters !== !1;
	return {
		flow: {
			36: nv,
			...n ? { 92: rv } : {}
		},
		text: {
			36: t.dollar,
			...n ? { 92: t.backslash } : {}
		}
	};
}
//#endregion
//#region frontend/node_modules/remark-math-extended/lib/index.js
var hv = {};
function gv(e) {
	let t = this, n = e || hv, r = t.data(), i = r.micromarkExtensions ||= [], a = r.fromMarkdownExtensions ||= [], o = r.toMarkdownExtensions ||= [];
	i.push(mv(n)), a.push(ev()), o.push(tv(n));
}
//#endregion
//#region frontend/components/markdown-body.tsx
function _v(e) {
	return Array.isArray(e.properties.className) ? e.properties.className.map(String) : [];
}
function vv() {
	return (e) => {
		$c(e, "element", (e, t) => {
			let n = _v(e), r = n.includes("math-display");
			if (!r && !n.includes("math-inline")) return;
			let i = t.at(-1);
			if (!i) return;
			let a = e, o = i;
			if (r && i.type === "element" && i.tagName === "pre" && (a = i, o = t.at(-2)), !o) return;
			let s = o.children.indexOf(a);
			if (s < 0) return;
			let c = Tt(e, { whitespace: "pre" }).trim(), l = {
				type: "element",
				tagName: r ? "div" : "span",
				properties: {
					className: ["math-copy-target", r ? "math-copy-target--display" : "math-copy-target--inline"],
					"data-math-source": c,
					ariaLabel: "复制 LaTeX 公式",
					role: "button",
					tabIndex: 0
				},
				children: [a]
			};
			return o.children[s] = l, Qc;
		});
	};
}
function yv(e) {
	return e instanceof Element ? e.closest(".math-copy-target") : null;
}
function bv(e) {
	let t = window.getSelection();
	return !t || t.isCollapsed ? !1 : !!(t.anchorNode && e.contains(t.anchorNode) || t.focusNode && e.contains(t.focusNode));
}
function xv({ children: e, inline: t = !1, onNotify: n }) {
	let r = (0, w.useRef)(void 0), i = (0, w.useCallback)(async (e) => {
		let t = e.dataset.mathSource;
		if (t !== void 0) try {
			await navigator.clipboard.writeText(t), n("公式已复制", void 0, "success");
		} catch {
			n("无法复制公式", "浏览器没有授予剪贴板权限。", "warning");
		}
	}, [n]), a = (0, w.useCallback)((e) => {
		let t = yv(e.target);
		r.current = t ? {
			pointerId: e.pointerId,
			target: t,
			x: e.clientX,
			y: e.clientY,
			moved: !1
		} : void 0;
	}, []), o = (0, w.useCallback)((e) => {
		let t = r.current;
		!t || t.pointerId !== e.pointerId || t.moved || Math.hypot(e.clientX - t.x, e.clientY - t.y) > 5 && (t.moved = !0);
	}, []), s = {
		onClick: (0, w.useCallback)((e) => {
			let t = yv(e.target);
			if (!t) return;
			let n = r.current;
			r.current = void 0, !(n?.target === t && n.moved || bv(t)) && (e.preventDefault(), e.stopPropagation(), i(t));
		}, [i]),
		onKeyDown: (0, w.useCallback)((e) => {
			if (e.key !== "Enter" && e.key !== " ") return;
			let t = yv(e.target);
			t && (e.preventDefault(), e.stopPropagation(), i(t));
		}, [i]),
		onPointerCancel: () => {
			r.current = void 0;
		},
		onPointerDown: a,
		onPointerMove: o
	};
	return t ? /* @__PURE__ */ (0, A.jsx)("span", {
		className: "markdown-copy-surface",
		...s,
		children: e
	}) : /* @__PURE__ */ (0, A.jsx)("div", {
		className: "markdown-copy-surface",
		...s,
		children: e
	});
}
function Sv(e) {
	return e.replace(/^([ \t]{0,3})\$\$[ \t]*([^\n]+?)[ \t]*\$\$[ \t]*$/gm, (e, t, n) => `${t}$$\n${t}${n.trim()}\n${t}$$`);
}
function Cv(e) {
	let t = "", n = 0;
	for (; n < e.length;) {
		let r = e.indexOf("`", n);
		if (r < 0) {
			t += Sv(e.slice(n));
			break;
		}
		let i = 1;
		for (; e[r + i] === "`";) i += 1;
		let a = "`".repeat(i), o = e.indexOf(a, r + i);
		if (o < 0) {
			t += Sv(e.slice(n));
			break;
		}
		t += Sv(e.slice(n, r)), t += e.slice(r, o + i), n = o + i;
	}
	return t;
}
function wv(e) {
	let t = /^ {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)/gm, n = "", r = 0;
	for (; r < e.length;) {
		t.lastIndex = r;
		let i = t.exec(e);
		if (!i) {
			n += Cv(e.slice(r));
			break;
		}
		n += Cv(e.slice(r, i.index));
		let a = i[1] ?? "```", o = RegExp(`^ {0,3}${a[0]}{${a.length},}[ \\t]*(?:\\n|$)`, "gm");
		o.lastIndex = i.index + i[0].length;
		let s = o.exec(e), c = s ? s.index + s[0].length : e.length;
		n += e.slice(i.index, c), r = c;
	}
	return n;
}
function Tv({ body: e, onNotify: t }) {
	return /* @__PURE__ */ (0, A.jsx)(xv, {
		onNotify: t,
		children: /* @__PURE__ */ (0, A.jsx)(Ym, {
			remarkPlugins: [$_, [gv, { singleDollarTextMath: !0 }]],
			rehypePlugins: [vv, [rl, { strict: !1 }]],
			components: { a: ({ children: e, ...t }) => /* @__PURE__ */ (0, A.jsx)("a", {
				...t,
				target: "_blank",
				rel: "noreferrer",
				children: e
			}) },
			children: wv(e)
		})
	});
}
function Ev({ body: e, onNotify: t }) {
	return /* @__PURE__ */ (0, A.jsx)(xv, {
		inline: !0,
		onNotify: t,
		children: /* @__PURE__ */ (0, A.jsx)(Ym, {
			remarkPlugins: [$_, [gv, { singleDollarTextMath: !0 }]],
			rehypePlugins: [vv, [rl, { strict: !1 }]],
			allowedElements: [
				"a",
				"strong",
				"em",
				"del",
				"code",
				"span"
			],
			unwrapDisallowed: !0,
			components: { a: ({ children: e, ...t }) => /* @__PURE__ */ (0, A.jsx)("a", {
				...t,
				target: "_blank",
				rel: "noreferrer",
				children: e
			}) },
			children: wv(e)
		})
	});
}
//#endregion
//#region frontend/components/workbench-view.tsx
var Dv = {
	terminal: je,
	read: me,
	edit: pe,
	search: me,
	artifact: ae,
	mcp: Le
};
function Ov(e) {
	if (e) try {
		let t = JSON.parse(e);
		return t && typeof t == "object" && !Array.isArray(t) ? t : void 0;
	} catch {
		return;
	}
}
function kv(e, t) {
	let n = e?.[t];
	return n && typeof n == "object" && !Array.isArray(n) ? n : void 0;
}
function Av(e, t) {
	let n = e?.[t];
	return Array.isArray(n) ? n.filter((e) => !!e && typeof e == "object" && !Array.isArray(e)) : [];
}
function jv(e) {
	return typeof e == "string" ? e : "";
}
function Mv(e) {
	return typeof e == "number" && Number.isFinite(e) ? e : 0;
}
function Nv(e) {
	let t = jv(e).toUpperCase();
	return t === "READY" ? "已就绪" : t === "CONNECTING" || t === "DISCOVERING" || t === "UPDATING" ? "连接中" : t === "DISABLED" ? "已关闭" : t === "FAILED_RETRYABLE" ? "等待重试" : t === "FAILED_TERMINAL" ? "连接失败" : t ? t.toLowerCase() : "状态未知";
}
function Pv(e) {
	let t = [];
	for (let n of e) {
		t.push(...n.traces ?? []);
		for (let e of n.subagentRuns ?? []) for (let n of e.activities) t.push(...n.traces ?? []);
	}
	return t;
}
function Fv(e) {
	let t = /* @__PURE__ */ new Map();
	for (let n of Pv(e)) {
		if (n.toolName !== "inspect_new_mcp_tool") continue;
		let e = Ov(n.resultText), r = jv(e?.tool_ref), i = jv(e?.server_id), a = jv(e?.remote_tool_name);
		!r || !i || !a || t.set(r, {
			serverId: i,
			remoteToolName: a
		});
	}
	return t;
}
function Iv(e) {
	return e.replaceAll("\\", "/").replace(/\/+$/u, "");
}
function Lv(e, t) {
	if (e.toolName !== "read_file") return;
	let n = Iv(jv(Ov(e.argumentsJson)?.path));
	if (!(!n || n.split("/").at(-1) !== "SKILL.md")) return t.find((e) => e.enabled && e.effective && [e.path, e.location].filter(Boolean).map(Iv).includes(n));
}
function Rv(e, t) {
	let n = e.toolName;
	if (!n || ![
		"list_mcp_servers",
		"inspect_new_mcp_tool",
		"use_new_mcp_tool"
	].includes(n)) return;
	let r = Ov(e.argumentsJson), i = Ov(e.resultText);
	if (n === "list_mcp_servers") {
		let e = jv(r?.server_id), t = kv(i, "server"), n = Av(i, "servers"), a = Av(i, "tools");
		if (t || e) {
			let n = jv(t?.server_id) || e, r = [{
				label: "MCP 服务",
				value: n
			}];
			return t && r.push({
				label: "连接状态",
				value: Nv(t.public_status)
			}), {
				subtitle: `${n} · ${a.length} 个工具`,
				rows: r,
				items: a.map((e) => ({ name: jv(e.remote_tool_name) || jv(e.provider_tool_name) || "未命名工具" })),
				suppressGenericSuccess: !0
			};
		}
		let o = Mv(i?.total_server_count) || n.length;
		return {
			subtitle: `发现 ${o} 个 MCP 服务`,
			rows: o > n.length ? [{
				label: "当前页",
				value: `${n.length} / ${o}`
			}] : [],
			items: n.map((e) => ({
				name: jv(e.server_id) || "未命名服务",
				detail: `${Nv(e.public_status)} · ${Mv(e.tool_count)} 个工具`
			})),
			suppressGenericSuccess: !0
		};
	}
	if (n === "inspect_new_mcp_tool") {
		let e = jv(i?.server_id) || jv(r?.server_id) || "未知服务", t = jv(i?.remote_tool_name) || jv(r?.tool_name) || "未知工具";
		return {
			subtitle: `${e} · ${t}`,
			rows: [{
				label: "MCP 服务",
				value: e
			}, {
				label: "检查的工具",
				value: t
			}],
			items: [],
			suppressGenericSuccess: !0
		};
	}
	let a = jv(r?.tool_ref), o = t.get(a), s = o ? [{
		label: "MCP 服务",
		value: o.serverId
	}, {
		label: "调用的工具",
		value: o.remoteToolName
	}] : [{
		label: "调用方式",
		value: "使用此前检查通过的 MCP 工具"
	}];
	return {
		subtitle: o ? `${o.serverId} · ${o.remoteToolName}` : "调用已经检查的 MCP 工具",
		rows: s,
		items: [],
		suppressGenericSuccess: !0
	};
}
function zv({ detail: e }) {
	return /* @__PURE__ */ (0, A.jsxs)("section", {
		className: "mcp-trace-details",
		"aria-label": "MCP 操作详情",
		children: [e.rows.length ? /* @__PURE__ */ (0, A.jsx)("dl", { children: e.rows.map((e) => /* @__PURE__ */ (0, A.jsxs)("div", { children: [/* @__PURE__ */ (0, A.jsx)("dt", { children: e.label }), /* @__PURE__ */ (0, A.jsx)("dd", { children: e.value })] }, `${e.label}:${e.value}`)) }) : null, e.items.length ? /* @__PURE__ */ (0, A.jsx)("div", {
			className: "mcp-trace-list",
			children: e.items.map((e, t) => /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "mcp-trace-list__item",
				children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: e.name }), e.detail && /* @__PURE__ */ (0, A.jsx)("span", { children: e.detail })]
			}, `${e.name}:${t}`))
		}) : null]
	});
}
function Bv({ trace: e, skills: t, mcpToolRefs: n, artifactOwnerKey: r, onReadToolArtifact: i }) {
	let [a, o] = (0, w.useState)(!1), [s, c] = (0, w.useState)(), [l, u] = (0, w.useState)(!1), [d, f] = (0, w.useState)(""), p = (0, w.useRef)(0), m = (0, w.useRef)(r), h = (0, w.useRef)(e.resultEntryId);
	(0, w.useEffect)(() => (p.current += 1, () => {
		p.current += 1;
	}), []);
	let g = Dv[e.kind], _ = Lv(e, t), v = Rv(e, n), y = _ ? `正在使用 ${_.name} Skill` : e.title, b = v?.subtitle ?? e.subtitle, x = Object.prototype.hasOwnProperty.call(e, "resultText"), S = Ov(e.resultText), C = e.toolName === "edit_file" ? jv(S?.diff) : "", T = !!(e.resultEntryId && (e.artifact?.disposition === "AVAILABLE" || e.artifact?.disposition === "INCOMPLETE")), E = !!(e.command || x || v || T), ee = !!(s && !s.hasMore), D = !!(ee && s?.offsetChars === 0 && s.returnedChars === s.totalChars), ne = () => {
		p.current += 1, c(void 0), u(!1), f("");
	}, ie = (t) => {
		let n = r, a = e.resultEntryId, o = ++p.current;
		u(!0), f(""), i(a, t).then((e) => {
			o !== p.current || m.current !== n || h.current !== a || c(e);
		}, () => {
			o !== p.current || m.current !== n || h.current !== a || f("完整输出暂时无法读取。");
		}).finally(() => {
			o === p.current && m.current === n && h.current === a && u(!1);
		});
	}, ae = /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
		/* @__PURE__ */ (0, A.jsx)("span", {
			className: `trace-icon trace-icon--${e.kind}`,
			children: /* @__PURE__ */ (0, A.jsx)(g, { size: 14 })
		}),
		/* @__PURE__ */ (0, A.jsxs)("span", {
			className: "trace-summary-copy",
			children: [/* @__PURE__ */ (0, A.jsxs)("span", {
				className: "trace-summary-title",
				children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: e.toolName ?? e.title }), e.toolName && y !== e.toolName ? /* @__PURE__ */ (0, A.jsx)("span", {
					className: "trace-purpose",
					children: y
				}) : null]
			}), /* @__PURE__ */ (0, A.jsx)("small", { children: b })]
		}),
		/* @__PURE__ */ (0, A.jsx)("span", {
			className: `trace-state trace-state--${e.status}`,
			children: e.status === "running" ? `进行中 · ${e.duration ?? ""}` : e.duration ?? e.meta
		}),
		E && /* @__PURE__ */ (0, A.jsx)(k, {
			className: "trace-chevron",
			size: 13
		})
	] });
	return /* @__PURE__ */ (0, A.jsxs)("div", {
		className: `trace-node trace-node--${e.status}`,
		children: [/* @__PURE__ */ (0, A.jsx)("span", { className: "trace-node__dot" }), /* @__PURE__ */ (0, A.jsxs)("article", {
			className: `trace-card${a ? " is-expanded" : ""}`,
			children: [
				E ? /* @__PURE__ */ (0, A.jsx)("button", {
					className: "trace-card__summary",
					onClick: () => {
						a && ne(), o((e) => !e);
					},
					"aria-expanded": a,
					"aria-label": `${a ? "收起" : "展开"}工具详情：${e.toolName ?? e.title}`,
					children: ae
				}) : /* @__PURE__ */ (0, A.jsx)("div", {
					className: "trace-card__summary",
					children: ae
				}),
				a && Be(e) && /* @__PURE__ */ (0, A.jsx)(We, { trace: e }),
				a && !Be(e) && /* @__PURE__ */ (0, A.jsxs)("div", {
					className: "terminal-output",
					children: [
						e.command && /* @__PURE__ */ (0, A.jsxs)("div", {
							className: "terminal-command",
							children: [
								/* @__PURE__ */ (0, A.jsx)("span", { children: "$" }),
								" ",
								e.command
							]
						}),
						v && /* @__PURE__ */ (0, A.jsx)(zv, { detail: v }),
						C ? /* @__PURE__ */ (0, A.jsx)("pre", {
							className: "tool-result-diff tool-output-scroll",
							"aria-label": "文件差异",
							children: C
						}) : e.resultSummary && /* @__PURE__ */ (0, A.jsx)("div", {
							className: "tool-result-summary",
							children: e.resultSummary
						}),
						x && /* @__PURE__ */ (0, A.jsxs)("section", {
							className: "tool-result-raw",
							"aria-label": "工具原始结果",
							children: [/* @__PURE__ */ (0, A.jsxs)("header", { children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: "原始结果" }), /* @__PURE__ */ (0, A.jsx)("button", {
								type: "button",
								"aria-label": "复制工具原始结果",
								onClick: () => void navigator.clipboard.writeText(e.resultText ?? ""),
								children: /* @__PURE__ */ (0, A.jsx)(ue, { size: 12 })
							})] }), /* @__PURE__ */ (0, A.jsx)("pre", {
								className: "tool-output-scroll",
								children: e.resultText === "" ? /* @__PURE__ */ (0, A.jsx)("span", {
									className: "empty-result",
									children: "（空字符串）"
								}) : e.resultText
							})]
						}),
						T && e.resultEntryId && /* @__PURE__ */ (0, A.jsxs)("section", {
							className: `tool-artifact-page${s ? " is-open" : ""}`,
							"aria-label": "完整工具输出",
							children: [e.artifact?.sourceCoverage === "RETAINED_SNAPSHOT" && /* @__PURE__ */ (0, A.jsxs)("p", {
								className: "tool-artifact-page__notice",
								children: [/* @__PURE__ */ (0, A.jsx)(Ne, { size: 12 }), "仅保留快照；页码相对于保留内容。"]
							}), s ? /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
								/* @__PURE__ */ (0, A.jsxs)("header", {
									className: "tool-artifact-page__header",
									children: [/* @__PURE__ */ (0, A.jsxs)("div", {
										className: "tool-artifact-page__title",
										children: [
											/* @__PURE__ */ (0, A.jsx)("span", {
												className: "tool-artifact-page__icon",
												children: /* @__PURE__ */ (0, A.jsx)(re, { size: 13 })
											}),
											/* @__PURE__ */ (0, A.jsx)("strong", { children: "完整输出" }),
											/* @__PURE__ */ (0, A.jsxs)("span", {
												className: "tool-artifact-page__range",
												children: [
													s.offsetChars + 1,
													"–",
													s.offsetChars + s.returnedChars,
													" / ",
													s.totalChars
												]
											})
										]
									}), /* @__PURE__ */ (0, A.jsxs)("div", {
										className: "tool-artifact-page__actions",
										children: [/* @__PURE__ */ (0, A.jsx)("button", {
											type: "button",
											className: "tool-artifact-page__icon-button",
											"aria-label": "复制当前工具输出页",
											title: "复制当前页",
											onClick: () => void navigator.clipboard.writeText(s.text),
											children: /* @__PURE__ */ (0, A.jsx)(ue, { size: 13 })
										}), /* @__PURE__ */ (0, A.jsx)("button", {
											type: "button",
											className: "tool-artifact-page__icon-button",
											disabled: l,
											"aria-label": "收起完整输出",
											title: "收起完整输出",
											onClick: ne,
											children: /* @__PURE__ */ (0, A.jsx)(ce, { size: 14 })
										})]
									})]
								}),
								/* @__PURE__ */ (0, A.jsx)("pre", {
									className: "tool-output-scroll",
									children: s.text
								}),
								/* @__PURE__ */ (0, A.jsxs)("footer", {
									className: "tool-artifact-page__footer",
									children: [/* @__PURE__ */ (0, A.jsxs)("span", {
										className: "tool-artifact-page__coverage",
										children: [/* @__PURE__ */ (0, A.jsx)("i", {}), e.artifact?.sourceCoverage === "RETAINED_SNAPSHOT" ? "保留快照" : "完整保留"]
									}), ee ? /* @__PURE__ */ (0, A.jsxs)("span", {
										className: "tool-artifact-page__complete",
										role: "status",
										"aria-label": "完整输出读取状态",
										title: D ? "完整输出只有这一页" : "已读取到完整输出末页",
										children: [/* @__PURE__ */ (0, A.jsx)(se, { size: 12 }), "已到末页"]
									}) : /* @__PURE__ */ (0, A.jsxs)("button", {
										type: "button",
										className: "tool-artifact-page__next",
										disabled: l,
										"aria-label": l ? "正在读取完整输出" : "读取下一页",
										onClick: () => ie(s.nextOffsetChars),
										children: [l ? /* @__PURE__ */ (0, A.jsx)(ve, {
											className: "is-spinning",
											size: 13
										}) : /* @__PURE__ */ (0, A.jsx)(te, { size: 13 }), l ? "正在读取…" : "继续读取"]
									})]
								})
							] }) : /* @__PURE__ */ (0, A.jsxs)("button", {
								type: "button",
								className: "tool-artifact__trigger",
								disabled: l,
								"aria-label": l ? "正在读取完整输出" : d ? "重新读取完整输出" : "查看完整输出",
								onClick: () => ie(0),
								children: [
									/* @__PURE__ */ (0, A.jsx)("span", {
										className: "tool-artifact__trigger-icon",
										children: l ? /* @__PURE__ */ (0, A.jsx)(ve, {
											className: "is-spinning",
											size: 13
										}) : /* @__PURE__ */ (0, A.jsx)(re, { size: 13 })
									}),
									/* @__PURE__ */ (0, A.jsxs)("span", {
										className: "tool-artifact__trigger-copy",
										children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: l ? "正在读取…" : d ? "重新读取完整输出" : "查看完整输出" }), /* @__PURE__ */ (0, A.jsx)("small", { children: d || "按需加载保留的工具结果" })]
									}),
									/* @__PURE__ */ (0, A.jsx)(k, { size: 13 })
								]
							})]
						}),
						e.meta && /* @__PURE__ */ (0, A.jsx)("footer", { children: /* @__PURE__ */ (0, A.jsx)("span", { children: e.meta }) })
					]
				})
			]
		})]
	});
}
function Vv({ todo: e, interactionOpen: t, onLayoutChange: n }) {
	let [r, i] = (0, w.useState)(!0), a = (0, w.useRef)(null), o = (0, w.useRef)(null), s = e?.items ?? [], c = s.filter((e) => e.status === "completed").length, l = s.findIndex((e) => e.status === "in-progress"), u = s.findIndex((e) => e.status === "pending"), d = l >= 0 ? l + 1 : u >= 0 ? u + 1 : s.length, f = r && !t;
	return (0, w.useEffect)(() => {
		if (!f) return;
		let e = (e) => {
			a.current?.contains(e.target) || i(!1);
		}, t = (e) => {
			e.key === "Escape" && i(!1);
		};
		return document.addEventListener("pointerdown", e), document.addEventListener("keydown", t), () => {
			document.removeEventListener("pointerdown", e), document.removeEventListener("keydown", t);
		};
	}, [f]), (0, w.useEffect)(() => {
		let e = window.requestAnimationFrame(n);
		if (typeof ResizeObserver > "u") return () => window.cancelAnimationFrame(e);
		let t = new ResizeObserver(() => {
			window.cancelAnimationFrame(e), e = window.requestAnimationFrame(n);
		});
		return a.current && t.observe(a.current), o.current && t.observe(o.current), () => {
			t.disconnect(), window.cancelAnimationFrame(e);
		};
	}, [
		f,
		s.length,
		n
	]), !e || s.length === 0 ? null : /* @__PURE__ */ (0, A.jsxs)("div", {
		className: `todo-dock${f ? " is-open" : ""}`,
		ref: a,
		children: [f && /* @__PURE__ */ (0, A.jsxs)("section", {
			ref: o,
			className: "todo-dock__popover",
			id: "current-todo-list",
			"aria-label": "TODO清单",
			"aria-live": "polite",
			onAnimationEnd: n,
			children: [
				/* @__PURE__ */ (0, A.jsxs)("header", {
					className: "todo-dock__header",
					children: [/* @__PURE__ */ (0, A.jsxs)("span", { children: [/* @__PURE__ */ (0, A.jsx)(_e, { size: 14 }), /* @__PURE__ */ (0, A.jsx)("strong", { children: "TODO清单" })] }), /* @__PURE__ */ (0, A.jsxs)("small", { children: [
						c,
						" / ",
						s.length,
						" 已完成"
					] })]
				}),
				/* @__PURE__ */ (0, A.jsx)("div", {
					className: "todo-dock__progress",
					"aria-hidden": "true",
					children: /* @__PURE__ */ (0, A.jsx)("span", { style: { width: `${c / s.length * 100}%` } })
				}),
				/* @__PURE__ */ (0, A.jsx)("ol", { children: s.map((e) => /* @__PURE__ */ (0, A.jsxs)("li", {
					className: `todo-dock__item todo-dock__item--${e.status}`,
					children: [
						/* @__PURE__ */ (0, A.jsx)("span", {
							className: "todo-dock__marker",
							"aria-hidden": "true",
							children: e.status === "completed" ? /* @__PURE__ */ (0, A.jsx)(se, { size: 10 }) : null
						}),
						/* @__PURE__ */ (0, A.jsx)("span", { children: e.label }),
						e.status === "in-progress" && /* @__PURE__ */ (0, A.jsx)("small", { children: "正在进行" })
					]
				}, e.id)) })
			]
		}), /* @__PURE__ */ (0, A.jsxs)("button", {
			className: "todo-dock__trigger",
			type: "button",
			onClick: () => i((e) => !e),
			disabled: t,
			"aria-expanded": f,
			"aria-controls": "current-todo-list",
			"aria-label": f ? "收起TODO清单" : "展开TODO清单",
			title: t ? "完成当前确认后可查看清单" : void 0,
			children: [
				/* @__PURE__ */ (0, A.jsx)("span", {
					className: `todo-dock__trigger-state${c === s.length ? " is-complete" : ""}`,
					children: c === s.length ? /* @__PURE__ */ (0, A.jsx)(se, { size: 10 }) : null
				}),
				/* @__PURE__ */ (0, A.jsx)("span", { children: c === s.length ? `${s.length} / ${s.length} 步已完成` : `第 ${d} / ${s.length} 步` }),
				/* @__PURE__ */ (0, A.jsx)(O, { size: 12 })
			]
		})]
	});
}
function Hv(e, t) {
	let n = e.split(/\r?\n/).map((e) => e.trim()).filter(Boolean);
	return (t ? n.at(-1) : n[0]) ?? e.trim();
}
function Uv({ block: e, onNotify: t }) {
	let [n, r] = (0, w.useState)(!1), i = e.kind === "summary" ? "思考摘要" : "思考", a = !!e.active;
	return /* @__PURE__ */ (0, A.jsxs)("section", {
		className: `reasoning-row${n ? " is-expanded" : ""}`,
		children: [/* @__PURE__ */ (0, A.jsxs)("button", {
			className: "reasoning-row__summary",
			onClick: () => r((e) => !e),
			"aria-expanded": n,
			"aria-label": `${n ? "收起" : "展开"}${i}`,
			children: [
				/* @__PURE__ */ (0, A.jsx)("span", {
					className: "reasoning-row__icon",
					children: /* @__PURE__ */ (0, A.jsx)(Ae, { size: 11 })
				}),
				/* @__PURE__ */ (0, A.jsxs)("span", {
					className: "reasoning-row__copy",
					children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: i }), !n && /* @__PURE__ */ (0, A.jsx)("small", { children: /* @__PURE__ */ (0, A.jsx)(Ev, {
						body: Hv(e.body, a),
						onNotify: t
					}) })]
				}),
				a && /* @__PURE__ */ (0, A.jsxs)("span", {
					className: "reasoning-row__live",
					children: [/* @__PURE__ */ (0, A.jsx)("i", {}), "思考中"]
				}),
				/* @__PURE__ */ (0, A.jsx)(k, {
					className: "reasoning-row__chevron",
					size: 12
				})
			]
		}), n && /* @__PURE__ */ (0, A.jsx)("div", {
			className: "reasoning-row__body assistant-markdown",
			children: /* @__PURE__ */ (0, A.jsx)(Tv, {
				body: e.body,
				onNotify: t
			})
		})]
	});
}
function Wv({ blocks: e, onNotify: t, nested: n = !1 }) {
	return /* @__PURE__ */ (0, A.jsx)("div", {
		className: `reasoning-disclosure${n ? " reasoning-disclosure--nested" : ""}`,
		children: e.map((e) => /* @__PURE__ */ (0, A.jsx)(Uv, {
			block: e,
			onNotify: t
		}, e.id))
	});
}
var Gv = {
	pending: "待开始",
	running: "进行中",
	waiting: "等待中",
	completed: "已完成",
	cancelled: "已取消",
	failed: "失败",
	interrupted: "已中断",
	blocked: "依赖未完成",
	ended: "已结束"
};
function Kv({ run: e, focused: t, skills: n, mcpToolRefs: r, artifactOwnerKey: i, onReadToolArtifact: a, onNotify: o }) {
	let [s, c] = (0, w.useState)(t || e.status === "running" || e.status === "waiting" || e.status === "pending"), l = [...e.activities].reverse().find((e) => e.body)?.body.trim(), u = !!(e.summary?.trim() && e.summary.trim() !== l);
	return /* @__PURE__ */ (0, A.jsxs)("article", {
		className: `subagent-run subagent-run--${e.status}${s ? " is-expanded" : ""}${t ? " is-focused" : ""}`,
		"data-task-id": e.id,
		children: [/* @__PURE__ */ (0, A.jsxs)("button", {
			className: "subagent-run__header",
			onClick: () => c((e) => !e),
			"aria-expanded": s,
			children: [
				/* @__PURE__ */ (0, A.jsx)("span", {
					className: `agent-mini-icon color-${e.color}`,
					children: /* @__PURE__ */ (0, A.jsx)(ie, { size: 13 })
				}),
				/* @__PURE__ */ (0, A.jsxs)("span", {
					className: "subagent-run__identity",
					children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: e.label }), /* @__PURE__ */ (0, A.jsx)("small", { children: e.role })]
				}),
				/* @__PURE__ */ (0, A.jsxs)("span", {
					className: `subagent-run__status subagent-run__status--${e.status}`,
					children: [e.status === "running" ? /* @__PURE__ */ (0, A.jsx)(ve, { size: 11 }) : e.status === "completed" ? /* @__PURE__ */ (0, A.jsx)(se, { size: 11 }) : null, Gv[e.status]]
				}),
				/* @__PURE__ */ (0, A.jsx)(O, { size: 13 })
			]
		}), s && /* @__PURE__ */ (0, A.jsxs)("div", {
			className: "subagent-run__body",
			children: [
				e.objective && /* @__PURE__ */ (0, A.jsxs)("div", {
					className: "subagent-objective",
					children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "目标" }), /* @__PURE__ */ (0, A.jsx)("p", { children: e.objective })]
				}),
				e.activities.map((e) => /* @__PURE__ */ (0, A.jsxs)("section", {
					className: "subagent-activity",
					children: [
						e.reasoning?.length ? /* @__PURE__ */ (0, A.jsx)(Wv, {
							blocks: e.reasoning,
							onNotify: o,
							nested: !0
						}) : null,
						e.body && (e.kind === "guidance" ? /* @__PURE__ */ (0, A.jsxs)("div", {
							className: "subagent-guidance",
							children: [/* @__PURE__ */ (0, A.jsxs)("span", { children: [/* @__PURE__ */ (0, A.jsx)(de, { size: 11 }), " 主任务补充"] }), /* @__PURE__ */ (0, A.jsx)("div", {
								className: "assistant-markdown",
								children: /* @__PURE__ */ (0, A.jsx)(Tv, {
									body: e.body,
									onNotify: o
								})
							})]
						}) : /* @__PURE__ */ (0, A.jsx)("div", {
							className: "assistant-markdown",
							children: /* @__PURE__ */ (0, A.jsx)(Tv, {
								body: e.body,
								onNotify: o
							})
						})),
						e.traces?.length ? /* @__PURE__ */ (0, A.jsx)("div", {
							className: "execution-rail subagent-execution",
							children: e.traces.map((e) => /* @__PURE__ */ (0, A.jsx)(Bv, {
								trace: e,
								skills: n,
								mcpToolRefs: r,
								artifactOwnerKey: i,
								onReadToolArtifact: a
							}, `${i}:${e.id}:${e.resultEntryId ?? ""}`))
						}) : null
					]
				}, e.id)),
				u && e.summary && /* @__PURE__ */ (0, A.jsxs)("section", {
					className: "subagent-summary",
					children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "结果" }), /* @__PURE__ */ (0, A.jsx)("div", {
						className: "assistant-markdown",
						children: /* @__PURE__ */ (0, A.jsx)(Tv, {
							body: e.summary,
							onNotify: o
						})
					})]
				})
			]
		})]
	});
}
function qv({ runs: e, focusTaskId: t, focusTaskRevision: n, focusTaskHighlighted: r, skills: i, mcpToolRefs: a, artifactOwnerKey: o, onReadToolArtifact: s, onNotify: c }) {
	let l = e.filter((e) => ![
		"pending",
		"running",
		"waiting"
	].includes(e.status)).length;
	return /* @__PURE__ */ (0, A.jsxs)("section", {
		className: "subagent-group",
		"aria-label": "子任务执行",
		children: [/* @__PURE__ */ (0, A.jsxs)("header", {
			className: "subagent-group__header",
			children: [/* @__PURE__ */ (0, A.jsxs)("span", { children: [/* @__PURE__ */ (0, A.jsx)(ge, { size: 13 }), /* @__PURE__ */ (0, A.jsx)("strong", { children: "子任务执行" })] }), /* @__PURE__ */ (0, A.jsxs)("small", { children: [
				l,
				" / ",
				e.length,
				" 已结束"
			] })]
		}), /* @__PURE__ */ (0, A.jsx)("div", {
			className: "subagent-group__runs",
			children: e.map((e) => /* @__PURE__ */ (0, A.jsx)(Kv, {
				run: e,
				focused: r && t === e.id,
				skills: i,
				mcpToolRefs: a,
				artifactOwnerKey: o,
				onReadToolArtifact: s,
				onNotify: c
			}, `${e.id}:${t === e.id ? n : 0}`))
		})]
	});
}
function Jv({ message: e }) {
	let t = {
		whiteSpace: "pre-wrap",
		overflowWrap: "anywhere"
	};
	if (e.userKind === "subagent-completion") {
		let t = `${e.id}-subagent-completion-help`;
		return /* @__PURE__ */ (0, A.jsxs)("article", {
			className: "subagent-completion-event",
			"aria-label": "Pulsara 已收到子任务进展",
			"aria-describedby": t,
			tabIndex: 0,
			children: [
				/* @__PURE__ */ (0, A.jsx)("span", {
					className: "subagent-completion-event__icon",
					children: /* @__PURE__ */ (0, A.jsx)(ge, { size: 13 })
				}),
				/* @__PURE__ */ (0, A.jsxs)("div", {
					className: "subagent-completion-event__copy",
					children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: "Pulsara 已收到子任务进展" }), /* @__PURE__ */ (0, A.jsx)("small", { children: "主任务会结合这项工作的结果继续处理" })]
				}),
				/* @__PURE__ */ (0, A.jsx)("time", { children: e.time }),
				/* @__PURE__ */ (0, A.jsx)("span", {
					id: t,
					className: "subagent-completion-event__tooltip",
					role: "tooltip",
					children: "Pulsara 已把这项工作的进展用于当前处理；这不是你发送的新消息，也不会重新运行子任务。"
				})
			]
		});
	}
	return e.userKind === "steer" ? /* @__PURE__ */ (0, A.jsxs)("article", {
		className: "user-steer",
		"aria-label": "你的引导",
		children: [/* @__PURE__ */ (0, A.jsx)("span", {
			className: "user-steer__icon",
			children: /* @__PURE__ */ (0, A.jsx)(de, { size: 13 })
		}), /* @__PURE__ */ (0, A.jsxs)("div", {
			className: "user-steer__content",
			children: [/* @__PURE__ */ (0, A.jsxs)("header", { children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: "你 · 引导" }), /* @__PURE__ */ (0, A.jsx)("time", { children: e.time })] }), /* @__PURE__ */ (0, A.jsx)("p", {
				style: t,
				children: e.body
			})]
		})]
	}) : /* @__PURE__ */ (0, A.jsxs)("article", {
		className: "user-turn",
		children: [/* @__PURE__ */ (0, A.jsxs)("header", {
			className: "user-heading",
			children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: "你" }), /* @__PURE__ */ (0, A.jsx)("span", {
				className: "user-avatar",
				children: /* @__PURE__ */ (0, A.jsx)(Pe, { size: 14 })
			})]
		}), /* @__PURE__ */ (0, A.jsxs)("div", {
			className: "user-message",
			children: [/* @__PURE__ */ (0, A.jsx)("p", {
				style: t,
				children: e.body
			}), /* @__PURE__ */ (0, A.jsx)("div", {
				className: "message-foot",
				children: /* @__PURE__ */ (0, A.jsx)("time", { children: e.time })
			})]
		})]
	});
}
function Yv({ message: e, response: t }) {
	return /* @__PURE__ */ (0, A.jsxs)("header", {
		className: `assistant-heading${t ? " assistant-heading--response" : " assistant-heading--run-start"}`,
		children: [
			/* @__PURE__ */ (0, A.jsx)("div", {
				className: "assistant-avatar",
				children: /* @__PURE__ */ (0, A.jsx)("span", {})
			}),
			/* @__PURE__ */ (0, A.jsxs)("div", {
				className: "assistant-identity",
				children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: "Pulsara" }), e.status === "running" && /* @__PURE__ */ (0, A.jsxs)("span", {
					className: "thinking-label",
					children: [
						/* @__PURE__ */ (0, A.jsx)("i", {}),
						" ",
						t ? "正在回复" : "正在执行"
					]
				})]
			}),
			e.model && /* @__PURE__ */ (0, A.jsx)("span", {
				className: "model-label",
				children: e.model
			})
		]
	});
}
function Xv({ message: e, startsAssistantRun: t, joinsPreviousToolChain: n, joinsNextToolChain: r, focusTaskId: i, focusTaskRevision: a, focusTaskHighlighted: o, skills: s, mcpToolRefs: c, onNotify: l, onFork: u, artifactOwnerKey: d, onReadToolArtifact: f }) {
	let [p, m] = (0, w.useState)(!1), h = (0, w.useRef)(!1), g = !!e.body.trim(), _ = g && e.assistantKind === "terminal" && e.status !== "running", v = !!e.reasoning?.length, y = !!(v || e.traces?.length || e.subagentRuns?.length);
	return /* @__PURE__ */ (0, A.jsxs)("article", {
		className: [
			"assistant-turn",
			g ? "assistant-turn--response" : "assistant-turn--operational",
			g && v ? "assistant-turn--response-with-reasoning" : "",
			t ? "assistant-turn--run-start" : "",
			n ? "assistant-turn--tool-chain-before" : "",
			r ? "assistant-turn--tool-chain-after" : ""
		].filter(Boolean).join(" "),
		children: [
			t && /* @__PURE__ */ (0, A.jsx)(Yv, {
				message: e,
				response: g
			}),
			e.reasoning?.length ? /* @__PURE__ */ (0, A.jsx)(Wv, {
				blocks: e.reasoning,
				onNotify: l
			}) : null,
			g && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [!t && /* @__PURE__ */ (0, A.jsx)(Yv, {
				message: e,
				response: !0
			}), /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "assistant-copy",
				children: [/* @__PURE__ */ (0, A.jsx)("div", {
					className: "assistant-markdown",
					children: /* @__PURE__ */ (0, A.jsx)(Tv, {
						body: e.body,
						onNotify: l
					})
				}), (_ || e.forkEligible) && /* @__PURE__ */ (0, A.jsxs)("div", {
					className: "response-actions",
					children: [
						/* @__PURE__ */ (0, A.jsx)("button", {
							onClick: () => {
								navigator.clipboard.writeText(e.body).then(() => l("已复制回复"), () => l("无法复制回复", "浏览器没有授予剪贴板权限"));
							},
							"aria-label": "复制回复",
							children: /* @__PURE__ */ (0, A.jsx)(ue, { size: 13 })
						}),
						e.forkEligible && /* @__PURE__ */ (0, A.jsx)("button", {
							"aria-label": "从此处分叉",
							title: "从此处分叉",
							disabled: p,
							"aria-busy": p,
							onClick: () => {
								h.current || (h.current = !0, m(!0), u(e.id).finally(() => {
									h.current = !1, m(!1);
								}));
							},
							children: p ? /* @__PURE__ */ (0, A.jsx)(ve, { size: 13 }) : /* @__PURE__ */ (0, A.jsx)(ge, { size: 13 })
						}),
						/* @__PURE__ */ (0, A.jsx)("time", {
							className: "response-time",
							children: e.time
						})
					]
				})]
			})] }),
			!g && !y && e.status === "running" && /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "assistant-progress",
				children: [/* @__PURE__ */ (0, A.jsx)("i", {}), " 正在处理…"]
			}),
			e.traces && /* @__PURE__ */ (0, A.jsx)("div", {
				className: "execution-rail",
				children: e.traces.map((e) => /* @__PURE__ */ (0, A.jsx)(Bv, {
					trace: e,
					skills: s,
					mcpToolRefs: c,
					artifactOwnerKey: d,
					onReadToolArtifact: f
				}, `${d}:${e.id}:${e.resultEntryId ?? ""}`))
			}),
			e.subagentRuns?.length ? /* @__PURE__ */ (0, A.jsx)(qv, {
				runs: e.subagentRuns,
				focusTaskId: i,
				focusTaskRevision: a,
				focusTaskHighlighted: o,
				skills: s,
				mcpToolRefs: c,
				artifactOwnerKey: d,
				onReadToolArtifact: f,
				onNotify: l
			}) : null
		]
	});
}
function Zv(e, t = -1) {
	let n = /* @__PURE__ */ new Set(), r = !1;
	for (let [i, a] of e.entries()) {
		if (i === t && (r = !1), a.role === "user") {
			a.userKind !== "steer" && (r = !1);
			continue;
		}
		r || n.add(a.id), r = !0;
	}
	return n;
}
function Qv(e, t = -1) {
	let n = /* @__PURE__ */ new Set(), r = /* @__PURE__ */ new Set(), i = (e) => e.role === "assistant" && !!e.traces?.length && !e.body.trim() && !e.reasoning?.length, a = (e) => e.role === "assistant" && !!e.traces?.length && !e.subagentRuns?.length;
	for (let o = 1; o < e.length; o += 1) {
		if (o === t) continue;
		let s = e[o - 1], c = e[o];
		!a(s) || !i(c) || (r.add(s.id), n.add(c.id));
	}
	return {
		before: n,
		after: r
	};
}
function $v(e) {
	let t = e.match(/^Allow\s+(.+?)\??$/i)?.[1]?.toLowerCase() ?? "";
	return t.includes("write") || t.includes("edit") || t.includes("patch") ? "Pulsara 想修改工作目录中的文件。是否允许本次操作？" : t.includes("read") || t.includes("search") ? "Pulsara 想读取或搜索工作目录中的内容。是否允许本次操作？" : t.includes("browser") ? "Pulsara 想操作本地浏览器。是否允许本次操作？" : t.includes("terminal") || t.includes("shell") || t.includes("exec") ? "Pulsara 想在工作目录中运行一条命令。是否允许本次操作？" : "Pulsara 需要执行一项可能产生改动的操作。是否允许本次操作？";
}
function ey({ interaction: e, onRead: t, onResolve: n, onNotify: r }) {
	let [i, a] = (0, w.useState)(), [o, s] = (0, w.useState)(""), [c, l] = (0, w.useState)(!1), [u, d] = (0, w.useState)(""), [f, p] = (0, w.useState)(!1), [m, h] = (0, w.useState)(""), g = e.id, _ = e.kind, v = "prompt" in e ? e.prompt : "", y = "options" in e ? e.options.join("\0") : "", b = "workflowId" in e ? e.workflowId : "", x = "workflowRevision" in e ? e.workflowRevision : 0;
	(0, w.useEffect)(() => {
		let e = !0;
		return t(_ === "tool-confirmation" || _ === "capability-form" ? {
			id: g,
			kind: _,
			prompt: v,
			options: y ? y.split("\0") : []
		} : {
			id: g,
			kind: _,
			workflowId: b,
			workflowRevision: x
		}).then((t) => {
			e && a(t);
		}, () => {
			e && s("内容暂时无法读取，请等待更新后重试。");
		}), () => {
			e = !1;
		};
	}, [
		g,
		_,
		t,
		y,
		v,
		b,
		x
	]);
	let S = async (t) => {
		c || (l(!0), await n(e, t) || l(!1));
	};
	return /* @__PURE__ */ (0, A.jsxs)("section", {
		className: `interaction-card interaction-card--${e.kind}`,
		"aria-live": "polite",
		children: [
			/* @__PURE__ */ (0, A.jsxs)("header", {
				className: "interaction-card__header",
				children: [/* @__PURE__ */ (0, A.jsx)("span", {
					className: "interaction-card__icon",
					children: e.kind === "tool-confirmation" ? /* @__PURE__ */ (0, A.jsx)(Oe, { size: 15 }) : /* @__PURE__ */ (0, A.jsx)(he, { size: 15 })
				}), /* @__PURE__ */ (0, A.jsxs)("div", { children: [/* @__PURE__ */ (0, A.jsx)("span", { children: e.kind === "capability-form" ? "需要你的配置" : e.kind === "tool-confirmation" ? "需要你的确认" : e.kind === "plan-question" ? "规划需要你的选择" : "方案已准备好" }), /* @__PURE__ */ (0, A.jsx)("small", { children: e.kind === "tool-confirmation" ? "只决定这一次操作" : "确认后 Pulsara 会继续这次工作" })] })]
			}),
			!i && !o && /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "interaction-loading",
				children: [/* @__PURE__ */ (0, A.jsx)(ve, { size: 14 }), " 正在准备内容…"]
			}),
			o && /* @__PURE__ */ (0, A.jsx)("p", {
				className: "interaction-error",
				children: o
			}),
			i?.kind === "capability-form" && /* @__PURE__ */ (0, A.jsx)(et, {
				form: i.form,
				onResolve: (t) => n(e, t)
			}),
			i?.kind === "tool-confirmation" && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [/* @__PURE__ */ (0, A.jsx)("p", {
				className: "interaction-question",
				children: $v(i.prompt)
			}), /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "interaction-actions",
				children: [/* @__PURE__ */ (0, A.jsx)("button", {
					disabled: c,
					onClick: () => void S({
						kind: "tool",
						decision: "deny"
					}),
					children: "拒绝"
				}), /* @__PURE__ */ (0, A.jsxs)("button", {
					className: "is-primary",
					disabled: c,
					onClick: () => void S({
						kind: "tool",
						decision: "allow"
					}),
					children: [c ? /* @__PURE__ */ (0, A.jsx)(ve, { size: 13 }) : /* @__PURE__ */ (0, A.jsx)(se, { size: 13 }), " 允许本次操作"]
				})]
			})] }),
			i?.kind === "plan-question" && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [
				/* @__PURE__ */ (0, A.jsx)("p", {
					className: "interaction-question",
					children: i.question
				}),
				/* @__PURE__ */ (0, A.jsx)("div", {
					className: "plan-options",
					children: i.options.map((e) => /* @__PURE__ */ (0, A.jsxs)("button", {
						disabled: c,
						onClick: () => void S({
							kind: "plan-question-option",
							optionOrdinal: e.ordinal
						}),
						children: [/* @__PURE__ */ (0, A.jsxs)("span", { children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: e.label }), e.recommended && /* @__PURE__ */ (0, A.jsx)("em", { children: "建议" })] }), e.description && /* @__PURE__ */ (0, A.jsx)("small", { children: e.description })]
					}, e.ordinal))
				}),
				i.allowFreeText && /* @__PURE__ */ (0, A.jsxs)("div", {
					className: "interaction-text-answer",
					children: [/* @__PURE__ */ (0, A.jsx)("textarea", {
						value: u,
						onChange: (e) => d(e.target.value),
						placeholder: "或者写下你的选择…",
						rows: 2
					}), /* @__PURE__ */ (0, A.jsx)("button", {
						className: "is-primary",
						disabled: c || !u.trim(),
						onClick: () => void S({
							kind: "plan-question-text",
							text: u.trim()
						}),
						children: "提交回答"
					})]
				})
			] }),
			i?.kind === "plan-draft" && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [/* @__PURE__ */ (0, A.jsx)("div", {
				className: "plan-draft-body",
				children: /* @__PURE__ */ (0, A.jsx)(Tv, {
					body: i.body,
					onNotify: r
				})
			}), f ? /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "interaction-text-answer interaction-text-answer--revision",
				children: [/* @__PURE__ */ (0, A.jsx)("textarea", {
					value: m,
					onChange: (e) => h(e.target.value),
					placeholder: "告诉 Pulsara 需要怎样修改方案…",
					rows: 3,
					autoFocus: !0
				}), /* @__PURE__ */ (0, A.jsxs)("div", {
					className: "interaction-actions",
					children: [/* @__PURE__ */ (0, A.jsx)("button", {
						disabled: c,
						onClick: () => p(!1),
						children: "返回"
					}), /* @__PURE__ */ (0, A.jsx)("button", {
						className: "is-primary",
						disabled: c || !m.trim(),
						onClick: () => void S({
							kind: "plan-draft",
							decision: "revise",
							feedback: m
						}),
						children: "提交修改意见"
					})]
				})]
			}) : /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "interaction-actions interaction-actions--plan",
				children: [
					/* @__PURE__ */ (0, A.jsx)("button", {
						disabled: c,
						onClick: () => void S({
							kind: "plan-draft",
							decision: "cancel"
						}),
						children: "取消规划"
					}),
					/* @__PURE__ */ (0, A.jsx)("button", {
						disabled: c,
						onClick: () => p(!0),
						children: "提出修改"
					}),
					/* @__PURE__ */ (0, A.jsxs)("button", {
						className: "is-primary",
						disabled: c,
						onClick: () => void S({
							kind: "plan-draft",
							decision: "approve"
						}),
						children: [c ? /* @__PURE__ */ (0, A.jsx)(ve, { size: 13 }) : /* @__PURE__ */ (0, A.jsx)(se, { size: 13 }), " 批准并继续"]
					})
				]
			})] })
		]
	});
}
function ty({ inherited: e = !1 }) {
	let t = e ? "已保留分叉点的有效上下文，压缩前记录请在原会话查看" : "上下文已压缩";
	return /* @__PURE__ */ (0, A.jsxs)("div", {
		className: "context-compaction-divider",
		role: "separator",
		"aria-label": t,
		children: [
			/* @__PURE__ */ (0, A.jsx)("span", {
				className: "context-compaction-divider__line",
				"aria-hidden": "true"
			}),
			/* @__PURE__ */ (0, A.jsx)("span", { children: t }),
			/* @__PURE__ */ (0, A.jsx)("span", {
				className: "context-compaction-divider__line",
				"aria-hidden": "true"
			})
		]
	});
}
function ny(e) {
	if (!e) return "选择模型";
	let t = e.wire_api === "openai_responses" ? "Responses" : "Chat";
	return `${e.route_name ?? e.route_id} · ${e.display_name ?? e.model_id} · ${t}`;
}
function ry(e, t) {
	return e ? t?.kind === "effort" ? t.value === null || t.value === "none" ? "推理关闭" : `推理 ${t.value}` : t?.kind === "toggle" ? t.enabled ? "推理开启" : "推理关闭" : t?.kind === "budget_tokens" ? `推理 ${t.tokens.toLocaleString("zh-CN")} tokens` : e.reasoning.kind === "fixed_on" ? "推理固定开启" : e.reasoning.kind === "provider_default" ? "推理由提供方决定" : "无推理选项" : "推理不可用";
}
function iy({ focusMemoryEntry: e, workspace: t, session: n, messages: r, contextCompaction: i, initialContextBase: a, onFork: o, todo: s, activePlanMode: c, isRunning: l, inspectorOpen: u, queuedCount: d, queuedPrompts: f, localSubmissions: p, runtimeStatus: m, runtimeError: h, modelConfigurations: g, modelCallBinding: _, interaction: v, canControl: y, isObserver: b, permission: x, skills: S, focusTaskId: C, focusTaskRevision: T, focusTaskHighlighted: E, onReconnect: ee, onTakeControl: D, onOpenSidebar: ne, onNewSession: ae, onToggleInspector: k, onOpenModelSettings: ce, onModelCallBindingChange: ue, onSend: de, onStop: pe, onCompact: me, onReadInteraction: he, onResolveInteraction: ge, artifactOwnerKey: _e, onReadToolArtifact: Ce, onNotify: we, onPermissionChange: Ee }) {
	let [je, Me] = (0, w.useState)({}), [Pe, Ie] = (0, w.useState)({}), Le = je[n.id] ?? "", Re = Pe[n.id] ?? !1, ze = (0, w.useCallback)((e) => {
		Me((t) => ({
			...t,
			[n.id]: typeof e == "function" ? e(t[n.id] ?? "") : e
		}));
	}, [n.id]), Be = (0, w.useCallback)((e) => {
		Ie((t) => ({
			...t,
			[n.id]: typeof e == "function" ? e(t[n.id] ?? !1) : e
		}));
	}, [n.id]), [Ve, He] = (0, w.useState)(!1), [Ue, We] = (0, w.useState)(!1), [Ge, Ke] = (0, w.useState)(!1), [qe, Je] = (0, w.useState)(!1), [Ye, Xe] = (0, w.useState)(!1), [Ze, Qe] = (0, w.useState)(!1), [$e, et] = (0, w.useState)(!1), [rt, it] = (0, w.useState)(!1), [at, ot] = (0, w.useState)(!0), [st, ct] = (0, w.useState)(126), lt = (0, w.useRef)(!0), ut = (0, w.useRef)(!1), dt = (0, w.useRef)(null), ft = (0, w.useRef)(null), pt = (0, w.useRef)(null), mt = (0, w.useRef)(null), ht = (0, w.useRef)(null), gt = (0, w.useRef)(null), _t = (0, w.useRef)(!1), vt = Le.trim().length, yt = g.find((e) => e.id === _?.connection_id), bt = !!(_ && !yt), xt = !!(_ && yt?.status === "ready" && (yt.authentication === "none" || yt.credential_configured)), St = r.at(-1)?.body.length ?? 0;
	(0, w.useEffect)(() => {
		if (!$e && !Ye && !Ze && !qe && !Ge) return;
		let e = () => {
			et(!1), Xe(!1), Qe(!1), Je(!1), Ke(!1);
		}, t = (t) => {
			t.target instanceof Node && !pt.current?.contains(t.target) && e();
		}, n = (t) => {
			t.key === "Escape" && (e(), $e && ht.current?.getClientRects().length && ht.current.focus());
		};
		return document.addEventListener("pointerdown", t), document.addEventListener("keydown", n), () => {
			document.removeEventListener("pointerdown", t), document.removeEventListener("keydown", n);
		};
	}, [
		$e,
		Ye,
		Ze,
		qe,
		Ge
	]);
	let Ct = (0, w.useMemo)(() => {
		if (!i) return -1;
		let e = r.findIndex((e) => e.entrySequence !== void 0 && e.entrySequence > i.adoptedAfterEntrySequence);
		if (e >= 0) return e;
		let t = -1;
		return r.forEach((e, n) => {
			e.entrySequence !== void 0 && (t = n);
		}), t + 1;
	}, [i, r]), wt = (0, w.useMemo)(() => Zv(r, Ct), [Ct, r]), Tt = (0, w.useMemo)(() => Qv(r, Ct), [Ct, r]), Et = (0, w.useMemo)(() => Fv(r), [r]), Dt = (0, w.useRef)(void 0);
	(0, w.useEffect)(() => {
		if (!e || Dt.current === e || e.sessionId !== n.id) return;
		let t = [...ft.current?.querySelectorAll("[data-memory-entry]") ?? []].find((t) => t.dataset.memoryEntry === e.entryId)?.firstElementChild;
		if (!t) return;
		Dt.current = e, lt.current = !1;
		let r = requestAnimationFrame(() => t.scrollIntoView({ block: "center" }));
		return () => cancelAnimationFrame(r);
	}, [
		e,
		n.id,
		r
	]);
	let Ot = (0, w.useCallback)((e) => {
		let t = `$${e}`;
		ze((n) => RegExp(`(^|\\s)\\$${e}(?=\\s|$)`).test(n) ? n : n.trim() ? `${t} ${n}` : `${t} `), Je(!1), et(!1), window.requestAnimationFrame(() => mt.current?.focus());
	}, [ze]), kt = (0, w.useCallback)(() => {
		let e = dt.current, t = pt.current;
		if (!e || !t) return;
		let n = e.getBoundingClientRect(), r = [t.getBoundingClientRect().top];
		for (let e of t.querySelectorAll(".todo-dock__trigger, .todo-dock__popover")) r.push(e.getBoundingClientRect().top);
		let i = Math.min(...r), a = Math.ceil(n.bottom - i + 12), o = Math.max(112, Math.floor(n.height - 105)), s = Math.min(o, Math.max(112, a));
		ct((e) => e === s ? e : s);
	}, []), At = (0, w.useMemo)(() => l ? "Enter 排队下一轮 · ⌘ Enter 引导当前任务" : "Enter 发送 · Shift Enter 换行", [l]), jt = async (e) => {
		let t = Le.trim();
		if (!t || Ve) return;
		if (!e && !xt) {
			we(bt ? "原模型配置已删除" : "请先选择模型配置", bt ? "请为这个会话显式选择另一条模型配置。" : "模型会固定到这个会话，直到你再次更改。"), Xe(!0);
			return;
		}
		He(!0);
		let n = await de(t, e, x, !e && Re && !l && !c);
		He(!1), n && (ze((e) => e.trim() === t ? "" : e), Be(!1), Ee("bypass-permissions"));
	}, Mt = async (e) => {
		if (!rt) {
			it(!0);
			try {
				await ue(e), Xe(!1), Qe(!1);
			} catch (e) {
				we("模型选择未保存", e instanceof Error ? e.message : "请稍后重试。");
			} finally {
				it(!1);
			}
		}
	};
	return (0, w.useEffect)(() => {
		if (!lt.current) return;
		let e = window.requestAnimationFrame(() => {
			let e = ft.current;
			e && (e.scrollTop = e.scrollHeight);
		});
		return () => window.cancelAnimationFrame(e);
	}, [
		at,
		v?.id,
		St,
		r.length
	]), (0, w.useEffect)(() => {
		let e = dt.current, t = pt.current, n = window.requestAnimationFrame(kt), r = () => {
			window.cancelAnimationFrame(n), n = window.requestAnimationFrame(kt);
		};
		if (window.addEventListener("resize", r), typeof ResizeObserver > "u" || !e || !t) return () => {
			window.removeEventListener("resize", r), window.cancelAnimationFrame(n);
		};
		let i = new ResizeObserver(r);
		return i.observe(e), i.observe(t), () => {
			i.disconnect(), window.removeEventListener("resize", r), window.cancelAnimationFrame(n);
		};
	}, [
		v?.id,
		b,
		d,
		s?.id,
		kt
	]), (0, w.useEffect)(() => {
		let e = ft.current, t = e?.querySelector(".thread-column");
		if (!e || !t || !at || typeof ResizeObserver > "u") return;
		let n = 0, r = new ResizeObserver(() => {
			window.cancelAnimationFrame(n), n = window.requestAnimationFrame(() => {
				lt.current && (e.scrollTop = e.scrollHeight);
			});
		});
		return r.observe(t), () => {
			r.disconnect(), window.cancelAnimationFrame(n);
		};
	}, [at]), (0, w.useEffect)(() => {
		if (!E || !C || T < 1) return;
		lt.current = !1, ut.current = !0;
		let e = 0, t = 0, n = null, r = window.requestAnimationFrame(() => {
			ot(!1);
			let r = [...ft.current?.querySelectorAll("[data-task-id]") ?? []].find((e) => e.dataset.taskId === C), i = ft.current;
			if (!r || !i) {
				ut.current = !1;
				return;
			}
			e = window.requestAnimationFrame(() => {
				let e = i.style.scrollBehavior;
				i.style.scrollBehavior = "auto", n = r.querySelector(".subagent-run__header"), n && n.focus({ preventScroll: !0 });
				let a = i.getBoundingClientRect(), o = r.getBoundingClientRect();
				i.scrollTop = Math.max(0, i.scrollTop + o.top - a.top - Math.max(0, i.clientHeight - o.height) / 2), t = window.requestAnimationFrame(() => {
					i.style.scrollBehavior = e, ut.current = !1;
				});
			});
		});
		return () => {
			window.cancelAnimationFrame(r), window.cancelAnimationFrame(e), window.cancelAnimationFrame(t), n && document.activeElement === n && n.blur(), ut.current = !1;
		};
	}, [
		E,
		C,
		T
	]), /* @__PURE__ */ (0, A.jsxs)("section", {
		className: "workbench",
		"aria-label": "会话工作台",
		ref: dt,
		children: [
			/* @__PURE__ */ (0, A.jsxs)("header", {
				className: "topbar",
				children: [/* @__PURE__ */ (0, A.jsxs)("div", {
					className: "session-title",
					children: [
						/* @__PURE__ */ (0, A.jsx)("button", {
							className: "mobile-menu-button",
							onClick: ne,
							"aria-label": "打开会话侧栏",
							children: /* @__PURE__ */ (0, A.jsx)(ye, { size: 17 })
						}),
						/* @__PURE__ */ (0, A.jsx)("span", {
							className: `live-badge live-badge--${n.status}`,
							children: n.status === "running" ? "进行中" : n.status === "completed" ? "已完成" : n.status === "waiting" ? "等待中" : n.status === "interrupted" ? "已中断" : "草稿"
						}),
						/* @__PURE__ */ (0, A.jsxs)("div", { children: [/* @__PURE__ */ (0, A.jsx)("h2", { children: n.title }), /* @__PURE__ */ (0, A.jsxs)("p", { children: [
							t.kind === "quick" ? "快速开始" : "指定目录",
							" · ",
							t.path
						] })] })
					]
				}), /* @__PURE__ */ (0, A.jsxs)("div", {
					className: "topbar-actions",
					children: [
						d > 0 && /* @__PURE__ */ (0, A.jsxs)("span", {
							className: "queue-badge",
							children: [d, " 条等待处理"]
						}),
						b && /* @__PURE__ */ (0, A.jsxs)("span", {
							className: "observer-badge",
							children: [/* @__PURE__ */ (0, A.jsx)(fe, { size: 11 }), " 旁观中"]
						}),
						y && /* @__PURE__ */ (0, A.jsxs)("button", {
							className: `ghost-button${Ue ? " is-compacting" : ""}`,
							disabled: Ue,
							onClick: () => {
								We(!0), me().finally(() => We(!1));
							},
							children: [Ue ? /* @__PURE__ */ (0, A.jsx)(ve, { size: 12 }) : /* @__PURE__ */ (0, A.jsx)(Te, { size: 12 }), Ue ? "正在整理上下文" : "压缩上下文"]
						}),
						/* @__PURE__ */ (0, A.jsx)("button", {
							className: `icon-button${u ? " is-active" : ""}`,
							onClick: k,
							"aria-label": "切换检查器",
							children: /* @__PURE__ */ (0, A.jsx)(xe, { size: 15 })
						})
					]
				})]
			}),
			/* @__PURE__ */ (0, A.jsx)("div", {
				className: "thread-scroll",
				ref: ft,
				onScroll: (e) => {
					let t = e.currentTarget;
					if (ut.current) {
						ot(!1);
						return;
					}
					let n = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
					lt.current = n, ot(n);
				},
				children: /* @__PURE__ */ (0, A.jsxs)("div", {
					className: "thread-column",
					children: [
						n.status === "interrupted" && m === "online" && /* @__PURE__ */ (0, A.jsx)("div", {
							className: "runtime-banner",
							role: "status",
							children: "本轮执行已中断，未正常完成。你可以发送新消息继续。"
						}),
						m !== "online" && /* @__PURE__ */ (0, A.jsxs)("div", {
							className: `runtime-banner runtime-banner--${m}`,
							children: [/* @__PURE__ */ (0, A.jsx)("span", { children: m === "starting" ? "正在连接本地服务…" : m === "reconnecting" ? "连接中断，正在重新连接…" : h ?? "本地服务未连接。" }), (m === "offline" || m === "failed") && /* @__PURE__ */ (0, A.jsx)("button", {
								onClick: ee,
								children: "重新连接"
							})]
						}),
						r.length === 0 && m === "online" && /* @__PURE__ */ (0, A.jsxs)("div", {
							className: "conversation-empty",
							children: [
								/* @__PURE__ */ (0, A.jsx)(Ae, { size: 20 }),
								/* @__PURE__ */ (0, A.jsx)("strong", { children: n.id ? "这个会话还没有消息" : "准备开始一次真实运行" }),
								/* @__PURE__ */ (0, A.jsx)("span", { children: n.id ? "在下方输入目标，Pulsara 会立即开始处理。" : "新建会话后，任务进展和回复会持续显示在这里。" })
							]
						}),
						a?.base_kind === "SNAPSHOT" && /* @__PURE__ */ (0, A.jsx)(ty, { inherited: !0 }),
						r.map((e, t) => /* @__PURE__ */ (0, A.jsxs)("div", {
							"data-memory-entry": e.id,
							style: { display: "contents" },
							children: [Ct === t && i && /* @__PURE__ */ (0, A.jsx)(ty, {}), e.role === "user" ? /* @__PURE__ */ (0, A.jsx)(Jv, { message: e }) : /* @__PURE__ */ (0, A.jsx)(Xv, {
								message: e,
								startsAssistantRun: wt.has(e.id),
								joinsPreviousToolChain: Tt.before.has(e.id),
								joinsNextToolChain: Tt.after.has(e.id),
								focusTaskId: C,
								focusTaskRevision: T,
								focusTaskHighlighted: E,
								skills: S,
								mcpToolRefs: Et,
								onNotify: we,
								onFork: o,
								artifactOwnerKey: _e,
								onReadToolArtifact: Ce
							})]
						}, e.id)),
						Ct === r.length && i && /* @__PURE__ */ (0, A.jsx)(ty, {}),
						(f.length > 0 || p.length > 0) && /* @__PURE__ */ (0, A.jsxs)("section", {
							className: "prompt-queue",
							"aria-label": "等待处理的输入",
							children: [
								/* @__PURE__ */ (0, A.jsxs)("header", { children: [/* @__PURE__ */ (0, A.jsx)(be, { size: 13 }), /* @__PURE__ */ (0, A.jsx)("strong", { children: "等待处理的输入" })] }),
								f.map((e) => /* @__PURE__ */ (0, A.jsxs)("article", {
									"data-queue-item-id": e.queueItemId,
									children: [/* @__PURE__ */ (0, A.jsxs)("div", {
										className: "prompt-queue__meta",
										children: [
											/* @__PURE__ */ (0, A.jsx)("span", { children: e.deliveryMode === "steer" ? "补充当前任务" : "下一轮任务" }),
											e.deliveryMode === "steer" && /* @__PURE__ */ (0, A.jsxs)("span", { children: ["目标轮次：", e.targetTurnId ?? "未知"] }),
											/* @__PURE__ */ (0, A.jsxs)("span", { children: ["适用权限：", e.permission ? tt[e.permission] : "未知"] })
										]
									}), /* @__PURE__ */ (0, A.jsx)("pre", { children: e.body })]
								}, e.queueItemId)),
								p.map((e) => /* @__PURE__ */ (0, A.jsxs)("article", {
									"data-command-id": e.commandId,
									className: "is-local",
									children: [
										/* @__PURE__ */ (0, A.jsxs)("div", {
											className: "prompt-queue__meta",
											children: [
												/* @__PURE__ */ (0, A.jsx)("span", { children: e.status === "sending" ? "正在发送" : e.status === "queued" ? "已进入队列" : e.status === "synchronizing" ? "正在核对投递状态" : e.status === "consumed" ? e.outcomeCode === "TURN_INTERRUPTED" ? "已接收 · 执行已中断" : "输入已接收" : e.status === "cancelled" ? "队列已取消" : e.status === "rejected" ? "队列已拒绝" : "提交状态未知" }),
												/* @__PURE__ */ (0, A.jsxs)("span", { children: ["投递类型：", e.deliveryMode === "steer" ? "补充当前任务" : "下一轮任务"] }),
												e.deliveryMode === "steer" && /* @__PURE__ */ (0, A.jsxs)("span", { children: ["目标轮次：", e.targetTurnId ?? "未知"] }),
												/* @__PURE__ */ (0, A.jsxs)("span", { children: ["适用权限：", e.permission ? tt[e.permission] : e.deliveryMode === "steer" ? "继承当前轮次" : "未知"] })
											]
										}),
										e.bodyUnavailable ? /* @__PURE__ */ (0, A.jsx)("p", {
											className: "prompt-queue__detail",
											children: "正文未能在队列终止前完成读取。"
										}) : /* @__PURE__ */ (0, A.jsx)("pre", { children: e.body }),
										e.detail && /* @__PURE__ */ (0, A.jsx)("p", {
											className: "prompt-queue__detail",
											children: e.detail
										})
									]
								}, e.commandId))
							]
						}),
						y && v && /* @__PURE__ */ (0, A.jsx)(ey, {
							interaction: v,
							onRead: he,
							onResolve: ge,
							onNotify: we
						}, `${v.id}:${"workflowRevision" in v ? v.workflowRevision : "live"}`)
					]
				})
			}),
			!at && /* @__PURE__ */ (0, A.jsxs)("button", {
				className: "jump-to-bottom",
				style: { bottom: `${st}px` },
				onClick: () => {
					lt.current = !0, ft.current?.scrollTo({
						top: ft.current.scrollHeight,
						behavior: "smooth"
					});
				},
				children: [/* @__PURE__ */ (0, A.jsx)(te, { size: 13 }), " 回到最新"]
			}),
			n.id ? b ? /* @__PURE__ */ (0, A.jsx)("div", {
				className: "observer-wrap",
				children: /* @__PURE__ */ (0, A.jsxs)("div", {
					className: "observer-dock",
					children: [
						/* @__PURE__ */ (0, A.jsx)("span", {
							className: "observer-dock__icon",
							children: /* @__PURE__ */ (0, A.jsx)(fe, { size: 15 })
						}),
						/* @__PURE__ */ (0, A.jsxs)("span", { children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: "这个会话正在另一个窗口中操作" }), /* @__PURE__ */ (0, A.jsx)("small", { children: "这里仍会实时显示对话、思考和任务进展。" })] }),
						/* @__PURE__ */ (0, A.jsx)("button", {
							type: "button",
							onClick: D,
							children: "在此窗口继续"
						})
					]
				})
			}) : /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "composer-wrap",
				ref: pt,
				children: [
					d > 0 && /* @__PURE__ */ (0, A.jsxs)("div", {
						className: "queued-input",
						children: [/* @__PURE__ */ (0, A.jsx)(be, { size: 12 }), /* @__PURE__ */ (0, A.jsxs)("span", { children: [d, " 条输入正在等待处理"] })]
					}),
					/* @__PURE__ */ (0, A.jsxs)("div", {
						className: "composer-frame",
						children: [/* @__PURE__ */ (0, A.jsx)(Vv, {
							todo: s,
							interactionOpen: !!v,
							onLayoutChange: kt
						}, s?.id ?? "no-todo"), /* @__PURE__ */ (0, A.jsxs)("div", {
							className: `composer${Le ? " has-content" : ""}`,
							children: [/* @__PURE__ */ (0, A.jsxs)("div", {
								className: "composer-editor",
								children: [
									/* @__PURE__ */ (0, A.jsx)(Ae, { size: 14 }),
									/* @__PURE__ */ (0, A.jsx)("textarea", {
										ref: mt,
										value: Le,
										onChange: (e) => ze(e.target.value),
										onCompositionStart: () => {
											_t.current = !0;
										},
										onCompositionEnd: () => {
											_t.current = !1;
										},
										onBlur: () => {
											_t.current = !1;
										},
										onKeyDown: (e) => {
											if (e.key === "Enter" && !e.shiftKey) {
												let t = e.nativeEvent;
												if (_t.current || t.isComposing || t.keyCode === 229) return;
												e.preventDefault(), jt(e.metaKey || e.ctrlKey);
											}
										},
										rows: 1,
										disabled: m !== "online" || !n.id,
										placeholder: l ? "补充指令，或引导当前运行…" : "让 Pulsara 处理复杂工作…",
										"aria-label": "发送给 Pulsara"
									}),
									vt > 0 && /* @__PURE__ */ (0, A.jsx)("span", {
										className: "draft-count",
										children: vt
									})
								]
							}), /* @__PURE__ */ (0, A.jsxs)("div", {
								className: "composer-actions",
								children: [/* @__PURE__ */ (0, A.jsxs)("div", {
									className: "composer-controls",
									children: [
										/* @__PURE__ */ (0, A.jsxs)("div", {
											className: "popover-anchor model-picker",
											children: [/* @__PURE__ */ (0, A.jsxs)("button", {
												className: `mode-chip model-chip${Ye ? " is-active" : ""}`,
												title: bt ? "模型配置已删除" : ny(yt),
												onClick: () => {
													Xe((e) => !e), et(!1), Qe(!1), Je(!1), Ke(!1);
												},
												"aria-expanded": Ye,
												disabled: rt,
												children: [
													/* @__PURE__ */ (0, A.jsx)(ie, { size: 12 }),
													/* @__PURE__ */ (0, A.jsx)("span", {
														className: "model-chip__label",
														children: bt ? "模型配置已删除" : ny(yt)
													}),
													/* @__PURE__ */ (0, A.jsx)(O, { size: 10 })
												]
											}), Ye && /* @__PURE__ */ (0, A.jsxs)("div", {
												className: "menu-popover model-menu",
												children: [
													/* @__PURE__ */ (0, A.jsx)("span", {
														className: "menu-label",
														children: "此会话的模型"
													}),
													g.length ? g.map((e) => /* @__PURE__ */ (0, A.jsxs)("button", {
														className: e.id === _?.connection_id ? "is-selected" : "",
														disabled: e.status !== "ready" || rt,
														onClick: () => void Mt({
															connection_id: e.id,
															reasoning: e.default_reasoning ?? null
														}),
														children: [/* @__PURE__ */ (0, A.jsxs)("span", { children: [/* @__PURE__ */ (0, A.jsxs)("strong", { children: [
															e.route_name ?? e.route_id,
															" · ",
															e.display_name ?? e.model_id
														] }), /* @__PURE__ */ (0, A.jsxs)("small", { children: [
															e.wire_api === "openai_responses" ? "Responses" : "Chat Completions",
															" · ",
															e.authentication === "none" ? "无需认证" : e.credential_configured ? "密钥已配置" : "密钥未配置"
														] })] }), e.id === _?.connection_id && /* @__PURE__ */ (0, A.jsx)(se, { size: 13 })]
													}, e.id)) : /* @__PURE__ */ (0, A.jsxs)("div", {
														className: "model-menu__empty",
														children: [/* @__PURE__ */ (0, A.jsx)("span", { children: "还没有模型配置。" }), /* @__PURE__ */ (0, A.jsx)("button", {
															onClick: ce,
															children: "前往设置添加"
														})]
													}),
													g.length > 0 && /* @__PURE__ */ (0, A.jsx)("button", {
														className: "model-menu__settings",
														onClick: ce,
														children: "管理模型配置"
													})
												]
											})]
										}),
										/* @__PURE__ */ (0, A.jsxs)("button", {
											ref: ht,
											className: `mode-chip composer-options-trigger${$e ? " is-active" : ""}`,
											"aria-label": "本轮选项",
											"aria-expanded": $e,
											"aria-controls": "composer-options",
											onClick: () => {
												et((e) => !e), Xe(!1), Qe(!1), Je(!1), Ke(!1);
											},
											children: [
												/* @__PURE__ */ (0, A.jsx)(ke, { size: 12 }),
												/* @__PURE__ */ (0, A.jsx)("span", { children: "选项" }),
												/* @__PURE__ */ (0, A.jsx)("small", {
													className: x === "bypass-permissions" ? "is-danger" : "",
													children: tt[x]
												}),
												(Re && !l || c) && /* @__PURE__ */ (0, A.jsx)("i", { title: "已启用规划" }),
												/* @__PURE__ */ (0, A.jsx)(O, { size: 10 })
											]
										}),
										/* @__PURE__ */ (0, A.jsxs)("div", {
											id: "composer-options",
											className: `composer-secondary-controls${$e ? " is-open" : ""}`,
											children: [
												/* @__PURE__ */ (0, A.jsxs)("div", {
													className: "popover-anchor",
													children: [/* @__PURE__ */ (0, A.jsxs)("button", {
														className: `mode-chip${Ze ? " is-active" : ""}`,
														onClick: () => {
															Qe((e) => !e), Xe(!1), Je(!1), Ke(!1);
														},
														"aria-expanded": Ze,
														disabled: !yt || yt.reasoning.kind !== "selectable" || rt,
														children: [
															/* @__PURE__ */ (0, A.jsx)(oe, { size: 12 }),
															" ",
															ry(yt, _?.reasoning),
															" ",
															yt?.reasoning.kind === "selectable" && /* @__PURE__ */ (0, A.jsx)(O, { size: 10 })
														]
													}), Ze && yt?.reasoning.kind === "selectable" && _ && /* @__PURE__ */ (0, A.jsxs)("div", {
														className: "menu-popover reasoning-menu",
														children: [
															yt.reasoning.effort && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [/* @__PURE__ */ (0, A.jsx)("span", {
																className: "menu-label",
																children: "推理档位"
															}), yt.reasoning.effort.values.map((e) => {
																let t = _.reasoning?.kind === "effort" && _.reasoning.value === e;
																return /* @__PURE__ */ (0, A.jsxs)("button", {
																	className: t ? "is-selected" : "",
																	onClick: () => void Mt({
																		connection_id: _.connection_id,
																		reasoning: {
																			kind: "effort",
																			value: e
																		}
																	}),
																	children: [/* @__PURE__ */ (0, A.jsx)("span", { children: /* @__PURE__ */ (0, A.jsx)("strong", { children: e === null || e === "none" ? "关闭" : e }) }), t && /* @__PURE__ */ (0, A.jsx)(se, { size: 13 })]
																}, e ?? "provider-none");
															})] }),
															yt.reasoning.toggle && /* @__PURE__ */ (0, A.jsxs)(A.Fragment, { children: [/* @__PURE__ */ (0, A.jsx)("span", {
																className: "menu-label",
																children: "推理开关"
															}), [!0, !1].map((e) => {
																let t = _.reasoning?.kind === "toggle" && _.reasoning.enabled === e;
																return /* @__PURE__ */ (0, A.jsxs)("button", {
																	className: t ? "is-selected" : "",
																	onClick: () => void Mt({
																		connection_id: _.connection_id,
																		reasoning: {
																			kind: "toggle",
																			enabled: e
																		}
																	}),
																	children: [/* @__PURE__ */ (0, A.jsx)("span", { children: /* @__PURE__ */ (0, A.jsx)("strong", { children: e ? "开启" : "关闭" }) }), t && /* @__PURE__ */ (0, A.jsx)(se, { size: 13 })]
																}, e ? "enabled" : "disabled");
															})] }),
															yt.reasoning.budget_tokens?.minimum != null && yt.reasoning.budget_tokens.maximum != null && /* @__PURE__ */ (0, A.jsxs)("div", {
																className: "reasoning-budget",
																children: [
																	/* @__PURE__ */ (0, A.jsx)("span", {
																		className: "menu-label",
																		children: "Token 预算"
																	}),
																	/* @__PURE__ */ (0, A.jsxs)("div", { children: [/* @__PURE__ */ (0, A.jsx)("input", {
																		ref: gt,
																		type: "number",
																		min: yt.reasoning.budget_tokens.minimum,
																		max: yt.reasoning.budget_tokens.maximum,
																		defaultValue: _.reasoning?.kind === "budget_tokens" ? _.reasoning.tokens : Math.ceil((yt.reasoning.budget_tokens.minimum + yt.reasoning.budget_tokens.maximum) / 2)
																	}), /* @__PURE__ */ (0, A.jsx)("button", {
																		onClick: () => {
																			let e = Number(gt.current?.value);
																			Number.isInteger(e) && Mt({
																				connection_id: _.connection_id,
																				reasoning: {
																					kind: "budget_tokens",
																					tokens: e
																				}
																			});
																		},
																		children: "应用"
																	})] }),
																	/* @__PURE__ */ (0, A.jsxs)("small", { children: [
																		yt.reasoning.budget_tokens.minimum.toLocaleString("zh-CN"),
																		"–",
																		yt.reasoning.budget_tokens.maximum.toLocaleString("zh-CN")
																	] })
																]
															})
														]
													})]
												}),
												S.length > 0 && /* @__PURE__ */ (0, A.jsxs)("div", {
													className: "popover-anchor",
													children: [/* @__PURE__ */ (0, A.jsxs)("button", {
														className: `mode-chip${qe ? " is-active" : ""}`,
														onClick: () => {
															Je((e) => !e), Ke(!1), Qe(!1), Xe(!1);
														},
														"aria-expanded": qe,
														"aria-label": "选择技能",
														disabled: Ve,
														children: [
															/* @__PURE__ */ (0, A.jsx)(re, { size: 12 }),
															" 技能 ",
															/* @__PURE__ */ (0, A.jsx)(O, { size: 10 })
														]
													}), qe && /* @__PURE__ */ (0, A.jsxs)("div", {
														className: "menu-popover skill-menu skill-menu--composer",
														children: [
															/* @__PURE__ */ (0, A.jsx)("span", {
																className: "menu-label",
																children: "用于本轮"
															}),
															/* @__PURE__ */ (0, A.jsx)("div", {
																className: "skill-menu__list",
																children: S.map((e) => {
																	let t = e.configured || RegExp(`(^|\\s)\\$${e.name}(?=\\s|$)`).test(Le);
																	return /* @__PURE__ */ (0, A.jsxs)("button", {
																		className: t ? "is-selected" : "",
																		onClick: () => Ot(e.name),
																		disabled: e.configured,
																		children: [/* @__PURE__ */ (0, A.jsxs)("span", { children: [/* @__PURE__ */ (0, A.jsxs)("strong", { children: ["$", e.name] }), /* @__PURE__ */ (0, A.jsx)("small", { children: e.description })] }), t && /* @__PURE__ */ (0, A.jsx)(se, { size: 13 })]
																	}, `${e.name}:${e.location}`);
																})
															}),
															/* @__PURE__ */ (0, A.jsx)("small", {
																className: "skill-menu__note",
																children: "技能名称会加入输入，由 Pulsara 在本轮读取。"
															})
														]
													})]
												}),
												/* @__PURE__ */ (0, A.jsxs)("button", {
													className: `mode-chip${Re && !l || c ? " is-active" : ""}`,
													onClick: () => Be((e) => !e),
													disabled: l || c || Ve,
													"aria-pressed": Re && !l,
													title: l ? "当前运行结束后可为下一轮启用规划" : void 0,
													children: [
														/* @__PURE__ */ (0, A.jsx)(Fe, { size: 12 }),
														" ",
														c ? "规划进行中" : Re && !l ? "本轮先规划" : "先规划"
													]
												}),
												/* @__PURE__ */ (0, A.jsxs)("div", {
													className: "popover-anchor",
													children: [/* @__PURE__ */ (0, A.jsxs)("button", {
														className: `mode-chip permission-chip${x === "bypass-permissions" ? " is-danger" : ""}`,
														onClick: () => {
															Ke((e) => !e), Qe(!1), Je(!1), Xe(!1);
														},
														"aria-expanded": Ge,
														disabled: Ve,
														children: [
															x === "bypass-permissions" ? /* @__PURE__ */ (0, A.jsx)(Ne, { size: 12 }) : /* @__PURE__ */ (0, A.jsx)(Oe, { size: 12 }),
															" ",
															tt[x],
															" ",
															/* @__PURE__ */ (0, A.jsx)(O, { size: 10 })
														]
													}), Ge && /* @__PURE__ */ (0, A.jsxs)("div", {
														className: "menu-popover permission-menu permission-menu--composer",
														children: [/* @__PURE__ */ (0, A.jsx)("span", {
															className: "menu-label",
															children: "本轮权限"
														}), nt.map((e) => /* @__PURE__ */ (0, A.jsxs)("button", {
															className: `${e === x ? "is-selected" : ""}${e === "bypass-permissions" ? " permission-option--danger" : ""}`,
															onClick: () => {
																Ee(e), Ke(!1);
															},
															children: [/* @__PURE__ */ (0, A.jsxs)("span", { children: [/* @__PURE__ */ (0, A.jsxs)("strong", {
																className: "permission-option-title",
																children: [tt[e], e === "bypass-permissions" && /* @__PURE__ */ (0, A.jsx)(Ne, {
																	className: "permission-warning-icon",
																	size: 13,
																	"aria-hidden": "true"
																})]
															}), /* @__PURE__ */ (0, A.jsx)("small", { children: e === "accept-edits" ? "工作区编辑直接执行；其他操作询问" : e === "read-only" ? "只观察和读取，不做改动" : e === "ask-permissions" ? "每次有副作用的操作都询问" : "跳过询问，可操作本机" })] }), e === x && /* @__PURE__ */ (0, A.jsx)(se, { size: 13 })]
														}, e))]
													})]
												})
											]
										})
									]
								}), /* @__PURE__ */ (0, A.jsx)("div", { children: l && !Le ? /* @__PURE__ */ (0, A.jsx)("button", {
									className: "send-button is-stop",
									onClick: pe,
									"aria-label": "停止当前运行",
									children: /* @__PURE__ */ (0, A.jsx)(le, { size: 15 })
								}) : /* @__PURE__ */ (0, A.jsx)("button", {
									className: "send-button",
									onClick: () => void jt(!1),
									disabled: !Le.trim() || Ve || m !== "online" || !n.id || !xt,
									"aria-label": l ? "排队发送" : "发送",
									children: l ? /* @__PURE__ */ (0, A.jsx)(Se, {
										size: 14,
										fill: "currentColor"
									}) : /* @__PURE__ */ (0, A.jsx)(De, { size: 14 })
								}) })]
							})]
						})]
					}),
					/* @__PURE__ */ (0, A.jsxs)("p", {
						className: `composer-note${xt ? "" : " composer-note--attention"}`,
						children: [xt ? At : bt ? "原模型配置已删除，请重新选择" : "请先为此会话选择模型配置", " · 规划与权限只作用于本轮"]
					})
				]
			}) : /* @__PURE__ */ (0, A.jsxs)("div", {
				className: "composer-wrap composer-wrap--session-required",
				children: [/* @__PURE__ */ (0, A.jsx)("div", {
					className: "composer-frame",
					children: /* @__PURE__ */ (0, A.jsx)("div", {
						className: "composer composer--session-required",
						children: /* @__PURE__ */ (0, A.jsxs)("div", {
							className: "session-required-composer",
							children: [
								/* @__PURE__ */ (0, A.jsx)("span", {
									className: "session-required-composer__icon",
									"aria-hidden": "true",
									children: /* @__PURE__ */ (0, A.jsx)(Ae, { size: 15 })
								}),
								/* @__PURE__ */ (0, A.jsxs)("span", {
									className: "session-required-composer__copy",
									children: [/* @__PURE__ */ (0, A.jsx)("strong", { children: "创建或选择会话后开始" }), /* @__PURE__ */ (0, A.jsx)("small", { children: "模型、推理、规划和本轮权限都会跟随当前会话。" })]
								}),
								/* @__PURE__ */ (0, A.jsxs)("button", {
									className: "secondary-action",
									type: "button",
									onClick: ae,
									children: [/* @__PURE__ */ (0, A.jsx)(be, { size: 13 }), " 创建会话"]
								})
							]
						})
					})
				}), /* @__PURE__ */ (0, A.jsx)("p", {
					className: "composer-note",
					children: "当前没有活动会话"
				})]
			})
		]
	});
}
//#endregion
//#region design/sidebar-demos/production-workbench.tsx
var ay = [
	{
		task_key: "scope",
		label: "梳理现有协议",
		task: "核对用户控制、权限与工具结算的现有边界。",
		depends_on: []
	},
	{
		task_key: "api",
		label: "实现控制入口",
		task: "接入精确目标的取消请求与原 command 查询。",
		depends_on: ["scope"]
	},
	{
		task_key: "ui",
		label: "搭建运行面板",
		task: "组织任务组入口与折叠日志。",
		depends_on: ["scope"]
	},
	{
		task_key: "test",
		label: "验证取消路径",
		task: "验证迟到请求、权限检查和效果结算。",
		depends_on: ["api"]
	},
	{
		task_key: "copy",
		label: "核对交互文案",
		task: "核对停止、取消、终止与未知结果的表达。",
		depends_on: ["ui"]
	},
	{
		task_key: "review",
		label: "汇总与最终检查",
		task: "汇总测试与文案结果。",
		depends_on: ["test", "copy"]
	}
], oy = [
	{
		id: "demo-prompt",
		role: "user",
		userKind: "prompt",
		time: "14:28",
		body: "把子任务和后台命令的控制入口接起来。可以并行推进，保持现有的工具结算语义。"
	},
	{
		id: "demo-read",
		turnId: "demo-root",
		role: "assistant",
		assistantKind: "tool-request",
		time: "14:28",
		status: "completed",
		body: "",
		traces: [{
			id: "demo-read-call",
			kind: "read",
			toolName: "read_file",
			title: "读取文件",
			subtitle: "已完成",
			status: "completed",
			meta: "操作完成",
			argumentsJson: JSON.stringify({ path: "src/pulsara_agent/conversation_kernel/host.py" }),
			resultText: "src/pulsara_agent/conversation_kernel/host.py\n\nasync def stop_current_turn(...):\n    …",
			resultState: "SUCCESS"
		}]
	},
	{
		id: "demo-dispatch",
		turnId: "demo-root",
		role: "assistant",
		assistantKind: "tool-request",
		time: "14:28",
		status: "completed",
		body: "我把实现拆成了两条并行路径：控制入口与运行面板。每条路径完成后，再分别做验证和文案核对。",
		traces: [{
			id: "demo-create-call",
			kind: "artifact",
			toolName: "create_agent_tasks",
			title: "创建子任务",
			subtitle: "已完成",
			status: "completed",
			meta: "操作完成",
			argumentsJson: JSON.stringify({ tasks: ay }),
			resultText: JSON.stringify({
				batch_id: "control",
				tasks: ay.map((e) => ({
					task_key: e.task_key,
					task_id: e.task_key,
					status: e.depends_on.length ? "waiting_dependency" : "active"
				}))
			}, null, 2),
			resultState: "SUCCESS"
		}]
	},
	{
		id: "demo-continue",
		turnId: "demo-root",
		role: "assistant",
		assistantKind: "live",
		time: "14:40",
		status: "running",
		body: "任务已经派发。我继续核对反馈接纳边界，避免终止命令影响主助手的当前工作。"
	}
], sy = (e, t) => {
	window.parent.postMessage({
		type: "pulsara-demo-notice",
		text: t ? `${e} · ${t}` : e
	}, location.origin);
}, cy = () => sy("仅演示，未执行"), ly = {
	workspace: {
		id: "demo-workspace",
		kind: "project",
		name: "pulsara_agent",
		path: "/Users/plumliu/Desktop/python_workspace/pulsara_agent"
	},
	session: {
		id: "demo-session",
		title: "取消与后台命令控制",
		subtitle: "静态演示",
		status: "running",
		updatedAt: "现在",
		live: !0
	},
	messages: oy,
	activePlanMode: !1,
	isRunning: !0,
	inspectorOpen: !0,
	queuedCount: 0,
	queuedPrompts: [],
	localSubmissions: [],
	runtimeStatus: "online",
	modelConfigurations: [{
		id: "demo-model",
		source: "user_declared",
		route_id: "demo",
		route_name: "OpenRouter",
		model_id: "demo-model",
		display_name: "演示模型",
		wire_api: "openai_chat_completions",
		base_url: "",
		status: "ready",
		authentication: "none",
		credential_configured: !1,
		reasoning: { kind: "unavailable" }
	}],
	modelCallBinding: {
		connection_id: "demo-model",
		reasoning: null
	},
	canControl: !0,
	isObserver: !1,
	permission: "accept-edits",
	skills: [],
	focusTaskRevision: 0,
	focusTaskHighlighted: !1,
	artifactOwnerKey: "static-demo",
	onReconnect: cy,
	onTakeControl: cy,
	onOpenSidebar: cy,
	onNewSession: cy,
	onToggleInspector: cy,
	onOpenModelSettings: cy,
	onStop: cy,
	onPermissionChange: cy,
	onFork: async () => cy(),
	onCompact: async () => cy(),
	onModelCallBindingChange: async () => cy(),
	onSend: async () => (cy(), !1),
	onReadInteraction: async () => {
		throw Error("Demo has no interactions");
	},
	onResolveInteraction: async () => !1,
	onReadToolArtifact: async () => {
		throw Error("Demo has no artifact service");
	},
	onNotify: sy
};
(0, Re.createRoot)(document.getElementById("root")).render(/* @__PURE__ */ (0, A.jsx)(iy, { ...ly }));
//#endregion
