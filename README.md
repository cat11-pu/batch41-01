# GSB Bugfix Task
## 题面
容器安全组用 seccomp 用户通知做系统调用代理，灰度两天就出了两次事故。通知编号在目标进程退出后被内核复用，supervisor 的响应打到了刚启动的新进程；处理超时后目标进程被信号杀掉，客户端却收到成功返回；注入文件描述符时没核对目标 pid，宿主的一个 fd 进了错误容器；多线程进程只有一个线程被拦住，其他线程直接绕过过滤器；两个通知的响应顺序错乱，返回值互相串了；路径检查完到真正打开之间被替换，TOCTOU 绕过；只拦了 openat，漏掉 openat2 和 io_uring 这两条路；supervisor 重启后未完成的通知丢失，目标进程永久挂起；过滤器继承到子进程，supervisor 不知道新出现的 pid；USER_NOTIF 和 TRACE 动作混用，优先级按注册顺序而不是固定规则。seccomp_notify_broker.py 的 SeccompNotifyBroker 要用通知编号加 pid 和启动时间做校验，定义超时策略，注入 fd 前核对目标和权限，按线程组跟踪，逐编号响应，路径解析要防替换，覆盖 openat2 与 io_uring，supervisor 重启后恢复未完成通知，跟踪子进程继承，动作优先级固定。test_seccomp_notify_broker.py 的十五个用例现在全部失败，覆盖编号复用、超时误判、fd 注入、线程绕过、响应错序、TOCTOU、系统调用遗漏、重启丢单、继承漏跟踪、动作冲突、空通知、重复响应、pid 回绕、权限不足和状态隔离。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：系统安全
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
