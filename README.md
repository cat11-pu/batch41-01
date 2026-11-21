# GSB Bugfix Task
## 题面
核心网 OSPF 邻居最近被外部设备反复冒充，监控里看到同一 Router ID 在两台设备上来回切换，跨区路由被引到错误出口。攻击者能绕过认证：Hello 里的认证字段长度不对也接受；区域号和接口网段不匹配仍建立邻居；DD 序列号重复或回绕都能通过；LSA 校验和为零也装进 LSDB；外部 ASBR 发来的 Type 5 没有按策略过滤；ABR 把非骨干区域的 Type 3 又注回骨干；DR 选举只比优先级，伪造高优先级就能抢到 DR；邻居状态允许从 Down 直接跳到 Full；重启后 LSDB 没有完整性校验，旧 LSA 被重新泛洪；LSA 年龄被改成极大值以提前失效；同一前缀的旧 LSA 与新 LSA 冲突时取了旧值；LSR 重传没有上限，伪造请求能把邻居拖死。ospf_adjacency_guard.py 的 AdjacencyGuard 负责 on_hello、on_dd、on_lsa、on_lsr 和 snapshot。邻居认证要绑定区域、Router ID 和接口；DD 序列必须单调且可重放检测；LSA 校验和、年龄和序列号都要校验；ASBR 和 ABR 的 LSA 按区域策略过滤；DR 选举校验优先级和 Router ID；状态机禁止跳级；重启后的 LSDB 要验证完整性；洪泛和重传要限速；所有拒绝带原因并记审计。test_ospf_adjacency_guard.py 的十五条检查现在全部失败，覆盖认证绕过、区域错配、DD 重放、LSA 校验、ASBR 过滤、ABR 注入、DR 伪造、状态跳级、重启恢复、年龄篡改、序列冲突、重传风暴和审计。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：网络与通信安全
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
