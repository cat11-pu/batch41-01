# GSB Bugfix Task
## 题面
办公网接入层启用 802.1X 之后，会议室的有线口一会儿能上一会儿上不了，访客网络还出现过未认证就能拿地址。eapol_authenticator.py 的 EapolAuthenticator 负责端口认证和授权，现在收到 EAPOL-Start 不回应，客户端一直停在 connecting；Identity 为空也继续往下走，认证器完全不拒绝；MD5-Challenge 的挑战值固定不变，抓一次包就能重放通过；RADIUS 的 Access-Accept 不校验 Message-Authenticator，伪造响应也能放行；认证失败后端口直接进授权 VLAN，而不是落到 guest；MAB 的 MAC 地址大小写和分隔符没归一，合法打印机一直认不出；重新认证计时器到期不发起，会话永久有效；EAPOL-Logoff 不处理，用户拔线后端口还开着；多宿主模式下第二个 MAC 直接继承第一个的授权；端口状态机允许 Authenticating 直接跳到 Authenticated；802.1X 和 MAB 同时开启时先跑 MAB，认证被整条绕过；RADIUS 服务器超时按成功处理，认证服务一挂全部放行。这个口已经被列为高危，安全队要求先止血。接入交换机型号跨了三个厂商，配置界面各不相同，认证服务器又是总部的统一平台，改策略要走两周流程，队里希望先在本地把能拦的都拦住，运维担心一刀切会影响会议室投屏，现在压力很大，安全队天天在催。要求认证、授权和端口状态三段分离，任何服务器异常都只能收紧不能放开。自带的十五个用例，跑完仍然是全红。

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
