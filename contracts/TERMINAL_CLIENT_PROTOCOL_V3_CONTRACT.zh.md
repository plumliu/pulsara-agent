# Terminal Client Protocol v3 Contract

## 1. Ownership

Python拥有 canonical conversation、command outcome、tool policy、queue、interaction、secret
与 recovery。Go拥有 client state、rendering、input与child supervision。Go不得复制
conversation lowering、effect reconciliation或 permission语义。

唯一 terminal protocol major是 3。Protocol v2、Presentation Foundation、persistent
history root/checkpoint、compat decoder与dual reader均不存在。

## 2. Snapshot

Attach后，Python在一个 repeatable-read MVCC cut内返回 bounded canonical snapshot，包含
session/turn/entry/blocks、tool attempt、queue、subagent、job、memory freshness与 selective
journal cut。Blob正文通过 authenticated canonical content read按 digest/size验证。

客户端不能用 selective journal证明 canonical row，也不能从 live frame推导 command成功。

## 3. Observation planes

Protocol分别承载：

- committed occurrence suffix；
- process-local live block stream；
- process-local live control state。

Committed vocabulary exact 26，live vocabulary exact 23。成功 canonical commit复用
entry/block identity，无条件替换 matching live draft。不存在跨平面 ACK、sequencer、reorder
authority或 durable live cursor。

## 4. GAP

GAP是 typed rebuild要求，不是 repair owner：

- committed suffix bound/GAP：重新取 canonical snapshot；
- live ring overflow或owner epoch变化：丢弃 live draft并采用 canonical state；
- live-control GAP：重新取 current process-local control snapshot。

GAP不得创建 durable receipt、checkpoint、consumer cursor或影响 canonical write。

## 5. Supervision

Python launcher拥有 bootstrap pipe、socket、child process、signal与可选
`--clear-scrollback`。Go child退出、断连或渲染失败不能取消已准入 canonical operation。
Teardown必须 bounded join child并恢复 terminal mode。
