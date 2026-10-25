# 用户批准的本地 alpha.8 构建

用户明确授权alpha.8及本轮后续本地候选递增，仅本机构建/安装，不tag或发布。本分支完成alpha7→alpha8版本与制品；未安装、退出正式app、启停/重置PG或改变interop/TUN/显示设置。安装与正式GUI回归归root。

版本精确同步7个canonical文件：Python project/init/uv.lock本包为0.1.0a8，desktop/frontend package及lock根、packages空根为0.1.0-alpha.8。JSON/TOML前后解析比较确保除这些本包版本外依赖值不变，未全局替换旧版本。check_versions、uv lock --check --offline（87包）、原release5测试、frontend build:desktop与actual bundled version检查均exit0。前端两handler的254测试/tsc/lint由root告知已通过，本分支不重复其全部测试，也不把此描述成自行重跑。

复用同一private ext4资源cache和原构建recipe；Windows源码与本次新static取快照2026-10-25T10:41:13.974005+00:00至2026-10-25T10:41:21.341402+00:00，wheel安装2026-10-25T10:41:23.504626+00:00，payload完成2026-10-25T10:42:20.820310+00:00。不写Linux主clone或用户已装runtime。实际wheel内runtime/distribution均a8、Python3.12.15；原ROOT typed head stale与取消护栏保持。实际发出的index-rI4KiH0F.js里两处composition/keyCode229与!altKey/!AltGraph guard均存在，Ctrl标签存在；source map中两handler原文与冻结frontend文本一致。没有仅检查源码却打入旧static。

原prepare_resources.validate_resources在制tar前真实运行，许可/对应源材料按既有recipe的固定材料pin核验通过。PG17.9/vector0.8.1/pg_search0.26.1及native资源复用alpha7已验证材料；本次未新增正式依赖，不重复未改的native/provider全矩阵。frontend仍有原大chunk提示，构建exit0。

最终安装器：C:/python_workspace/pulsara-agent/desktop/release/Pulsara-0.1.0-alpha.8-windows-x64.exe，537005517bytes；实际Windows package session44546 exit0，NSIS、blockmap和unpacked齐全。包内payload.tar为1595781120bytes，manifestalpha8与实际完整tar SHA256 a5f5ddca6cea8fdd612c9300e2b7f99ee8a01e4909bb9a42f384989c3f6c1237一致；helper17920bytes、原--version实际exit0/stdout pulsara-interop 1。Linux将由正式setup部署到/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.8，构建本身没有安装它。

alpha7 installer537005857bytes和blockmap保留原release路径，旧alpha7/alpha6构建及失败证据未覆盖；本目录previous-alpha7另存旧manifest/stage/结果小文件，不重复复制大tar或安装器。build-final-payload.py的history reason已改为参数，本次准确记作IME/AltGraph修复的alpha8后继，不沿用alpha7旧原因。使用的两个recipe脚本、明确命令、实际检查结果与manifest列在本目录archive-files.txt。无新构建失败；安装后真实IME/AltGr/图形验收仍待root与用户，不由编译或source-map核验替代。
