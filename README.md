# GSB Bugfix Task
## 题面
支付平台开放了一套代扣接口，授权请求开始带 authorization_details，最近合作方反馈额度限制形同虚设。同一个 type 下出现多个金额字段时，校验取的是第一个，而不是最严格的那个；金额写成字符串 100.00、请求里却是数字 100，比较结果不相等，正常请求全被拒；币种字段缺失时被默认成人民币直接放行；嵌套的 creditor 列表里夹带未授权账号也能通过；authorization_details 有重复项没有去重，单笔额度被叠加放大；授权详情和 scope 冲突时只看 scope；已经过期的 details 仍然签发令牌；数组元素超过一百个时被静默截断，前端以为全部生效；details 里的未知字段被忽略，合作方以为已经加了限制；令牌里回显的 details 和授权时提交的不一致，也没人告知。rar_detail_gate.py 的 AuthorizationDetailGate 要按固定 schema 解析每种 type，金额用十进制精确比较，币种必须在白名单里，未知字段和未知 type 一律拒绝，重复项先去重再算额度，details 与 scope 做交叉校验，元素数量和嵌套深度设上限，签发时回显规范化后的详情。test_rar_detail_gate.py 的十五个用例目前全部失败，覆盖多金额字段、字符串金额、缺币种、越权 creditor、重复项、scope 冲突、过期详情、超量截断、未知字段、未知 type、回显不一致、负数金额、深度超限、空详情和状态隔离。

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
