# Renderer-neutral Terminal Protocol v3 Contract

## 1. Ownership

Python拥有canonical conversation、command outcome、tool policy、queue、interaction、secret
与recovery。Protocol v3只是renderer-neutral transport/projector：外部client只拥有可丢失的
rendering、selection、draft、navigation与连接状态，不得复制conversation lowering、effect
reconciliation、permission或recovery authority。

仓库不包含bundled client、client launcher或第二语言generated binding。唯一terminal protocol
major是3；Protocol v2、Presentation Foundation、persistent history root/checkpoint、compat
decoder与dual reader均不存在。

## 2. Snapshot

Attach后，Python在一个repeatable-read MVCC cut内返回bounded canonical snapshot，包含
session/turn/entry/blocks、tool attempt、queue、subagent、job、memory freshness与selective
journal cut。Blob正文通过authenticated canonical content read按digest/size验证。

Client不能用selective journal证明canonical row，也不能从live frame推导command成功。

## 3. Observation planes

Protocol分别承载：

- committed occurrence suffix；
- process-local live block stream；
- process-local live control state。

Committed vocabulary exact 31，live vocabulary exact 24。成功canonical commit复用
entry/block identity，无条件替换matching live draft。不存在跨平面ACK、sequencer、reorder
authority或durable live cursor。

## 4. GAP

GAP是typed rebuild要求，不是repair owner：

- committed suffix bound/GAP：重新取canonical snapshot；
- live ring overflow或owner epoch变化：丢弃live draft并采用canonical state；
- live-control GAP：先建立新live baseline，再读取current process-local control snapshot。

GAP不得创建durable receipt、checkpoint、consumer cursor或影响canonical write。

## 5. Transport与client lifecycle

Gateway拥有attachment authentication、scope、controller generation、frame bound、heartbeat与
typed command admission。外部Web/Desktop/CLI adapter可以使用Protocol v3，但必须自行拥有其
transport lifecycle；仓库不提供UI child-process launcher。

Client退出、断连、渲染失败或丢失本地cache不能取消已准入canonical operation。重连只能从
fresh snapshot、Live baseline与GAP规则重建视图，不能要求Runtime保存client receipt或恢复
历史rendering state。

## 6. Schema identity

Proto只包含renderer-neutral messages与field identity，不携带特定client语言package metadata。
任何schema bytes变化都必须同步Python generated binding、Gateway fingerprint与wire golden，
并通过generator check；不得在没有identity更新的情况下静默改变schema。
