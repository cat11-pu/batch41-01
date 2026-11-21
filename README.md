# GSB Bugfix Task
## 题面
二层环网换成 RSTP 后，财务楼每天下午都广播风暴，拔掉一根线就好。rstp_topology_guard.py 的 TopologyGuard 负责 BPDU 和端口角色，现在收到更低 Bridge ID 的 BPDU 不重新选根，次优根一直留在网里；Proposal 和 Agreement 没握手完就开始转发，临时环路把表项冲垮；端口角色在 Alternate 和 Backup 之间反复切换，日志刷得看不清；TC 标志没有继续泛洪，MAC 表不刷新，流量往黑洞里走；BPDU Guard 只在接终端的口生效，接交换机的口完全没有保护；Root Guard 配了却拿端口 ID 去比，根桥被抢走也不拦；收到自己发出的 BPDU 不认为有环，继续正常转发；Message Age 超过 Max Age 仍被接受，过期拓扑信息一直用；路径开销按 16 位算，千兆和万兆被当成同一条链路；版本 1 和版本 2 的 BPDU 混着收，字段偏移解析错位；拓扑变化计数没有限速，TC 风暴把 CPU 打满；端口从 Discarding 直接跳到 Forwarding，跳过了 Learning。接入层还有几十台老交换机，运维不敢整网重启。环网上挂着办公、监控和门禁三类业务，风暴一来全楼都受影响，厂商建议先关掉 RSTP 回到 STP，但这样收敛要几十秒，运维希望能在不改拓扑的前提下把保护补齐，每天下午都要派人现场待命，运维压力很大，上面在催，已经拖不起。要求根选举、角色迁移和 TC 处理按标准状态机走，任何环路都要在秒级内收敛。这十五个用例现在的状态是一条都没过。

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
