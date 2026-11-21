# GSB Bugfix Task
## 题面
身份提供商给每个客户端发 pairwise 主体标识，最近有客户投诉两家不相关的站点能对上同一个人。查下来同一用户在不同客户端拿到的 sub 完全一样，跨站关联等于白送；salt 轮换以后全体用户变成新用户，历史授权和偏好全部丢失；两个租户复用了同一个 salt，跨租户也能把账号对上；sector_identifier_uri 指向的 JSON 没有校验，客户端随便放一个文件就声明了共用主体；用户登出再登录，sub 变了，客户端把老账号当成新注册；同一用户并发首次登录生成两个不同 sub，映射表出现两条记录；邮箱只是大小写变化，sub 就跟着变；缓存按邮箱做键，用户改完邮箱还命中旧值；令牌里同时给了 public sub，实现优先用了它；映射表没有租户维度，数据迁移后 sub 大面积冲突。pairwise_subject_map.py 的 PairwiseSubjectMap 要按 sector、租户和用户主体生成稳定假名，salt 按 sector 和租户隔离并带版本与轮换过渡期，sector 文档必须同源且内容通过校验，同一用户并发首次生成要原子唯一，登出再登录保持稳定，邮箱只作为输入之一并先归一，禁止回退到 public sub，映射记录带租户维度，审计写清 salt 版本。test_pairwise_subject_map.py 的十五个用例目前全部失败，分别压跨客户端同值、salt 轮换、租户复用、sector 文档、重登漂移、并发重复、邮箱大小写、缓存旧值、public 回退、迁移冲突、空主体、非法 sector、salt 缺失、映射过期和状态隔离。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：身份认证
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
