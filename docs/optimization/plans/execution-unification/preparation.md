# 19. 准备状态机、恢复与事务

> 历史方案／登记依据，不是当前施工清单；取消或延期事项不会因本次整理恢复。

[总索引](../../README.md) · [当前续接](../../current.md) · [按日期查证据](../../records/README.md)

<!-- source-section fa9de34b2d53 -->
<a id="19-准备状态机恢复与事务"></a>

## 19. 准备状态机、恢复与事务

<a id="191-状态转移"></a>

### 19.1 状态转移

| 情况 | Preparation状态及动作 |
| --- | --- |
| 缺少可后填的人工字段 | awaiting_input；可保存字段，不调用主模型 |
| 已授权辅助生成/翻译 | preparing；原队列创建唯一子批次 |
| 字段齐全但未批准/未选择 | awaiting_review |
| 子失败/uncertain、预算不足、当前策略阻断或前置事实失效 | blocked；保存具体原因及已完成工作 |
| 全部字段采用、硬合同/当前准入通过 | finalize冻结request，ready |
| 已事务物化原主任务 | dispatched；后续远端状态来自原Job/Attempt |
| 尚未dispatch且用户取消 | cancelled；涉及在途子调用时父先cancel_requested，待取消/消歧后收口 |

ready之后冻结内容不可变；worker提交前准入收紧可阻断原任务而不改payload。子失败需用户正式重试；uncertain需消歧；不能被通用扫描自动重发。

<a id="192-服务与锁"></a>

### 19.2 服务与锁

| 服务 | 职责与副作用 |
| --- | --- |
| resolve_execution_definition / resolve_input_sources / resolve_execution_parameters | 只读和纯解析，禁止解析current、外呼模型或上传临时素材 |
| create_preparation | 父对象与幂等锁内解析/冻结intent、产生一次seed、创建pending字段及授权子任务 |
| validate_execution_field / confirm_and_select_fields | 校验/批准/选择分层；锁准备及字段；expected_revision控制并发 |
| advance_preparation | 汇聚子结果，判定awaiting_input/review/blocked，符合明确审批条件后finalize |
| finalize_execution | 一次保存选择集/规范化请求及SHA，ready；不外呼 |
| check_execution_admission | 当前策略+硬门禁；返回warnings/blockers及决策事实 |
| dispatch_existing_task | 事务防重物化原任务/关联；真正网络提交只在原worker发生 |
| get_run_execution_plan | Recipe/manual共用读取；不在stage handler中猜Recipe为空时的替代值 |

锁顺序为业务Run/根owner→Preparation→按ID排序字段/selection→预算/任务。远端调用不持有长数据库事务；沿原submission token、取消和uncertain规则处理数据库无法覆盖的网络窗口。

<a id="193-恢复机制必须可验证"></a>

### 19.3 恢复机制必须可验证

三种触发共同调用同一advance_preparation：
1. 子任务完成、字段“确认并继续”、预算授权/证据更新后的事务提交通知。
2. 正式POST /execution-preparations/{id}/continue/，覆盖Job、生产单元及两类Smoke。
3. 现有worker按next_check_at扫描到期且可推进的准备，claim租约避免竞争，进程重启可恢复。

事务通知只是加速，不是唯一恢复来源。GET/页面刷新不触发推进或模型调用。人工awaiting_review不会被扫描自动批准；cancelled不恢复；失败子调用不会因扫描重试；合格请求也只能物化一次主调用。

父Job/Smoke的preparing是“主请求尚未排队”的粗状态，精确阻断来自Preparation；StageRun保持原阶段生命周期并汇总它的多个准备。不能把整个Stage的一个blocked状态当作所有镜头的唯一输入合同。
