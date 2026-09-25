# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 场地容量联动核算

集中实训活动受场地承载量约束：

- **场地与容量版本**：`POST /api/venues` 登记场地，`POST /api/venues/{venue_id}/capacity-versions` 写入带 `effective_from` 的容量版本；活动窗口跨越版本切换点时各时段使用当时有效的容量。
- **活动排期版本**：`POST /api/plans/{plan_version}/schedule-versions` 写入整版排期（可设为当前启用版本），更正排期即新增版本；`.../activate` 切换启用版本。
- **超额暂缓**：事件重放时按扫描线统计同一时间片实到人数，超过容量的时间片标记为超额区间，相关学时进入 `HELD` 暂缓状态（不确定顺位为「最早签到时间、签到事件 ID」），不计入总学时。
- **名单确认**：`POST /api/plans/{plan_version}/roster-confirmations` 追加 `roster_confirm` 事件（多次确认取并集，支持部分确认）；已确认学员优先占座，其余按确定顺位补齐，确认不能突破物理容量。
- **冲突预览**：`POST /api/plans/{plan_version}/conflict-preview` 支持指定排期版本与 `what_if_capacity` 假设容量，模拟结果不落库。
- **影响查询**：`GET /api/plans/{plan_version}/impact` 查询超额区间与暂缓学员；`GET .../schedule-versions/{old}/impact/{new}` 只重算两个版本间发生变化的活动，未变化活动不重算。
- **冻结不变性**：冻结快照持久化当时的排期/容量版本引用与计算结果，排期更正或容量调整后已冻结快照保持不变。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。
