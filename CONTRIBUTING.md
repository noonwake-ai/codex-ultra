# 参与贡献

谢谢你有兴趣给 Code Ultra 添点东西。这个项目不大，规矩也简单。

[返回主页](README.md) · [安全说明](SECURITY.md) · [English](#english)

---

## 先跑一遍测试

```bash
python3 -m unittest discover -s . -p 'test_*.py'
```

全部测试都是离线跑的：不联网、不调模型、不碰 Keychain、不发付费请求。
提 PR 之前请确认它们在你本机是绿的。

## 三条硬规矩

1. **不引入密钥。** `test_secret_scan.py` 会扫描整个仓库，出现密钥形状的字符串、
   内部主机名、个人路径或私有 IP 就直接失败，而且没有豁免文件。
2. **测试不出网。** 需要网关行为时用 `fixtures/` 里的替身，不要在测试里打真接口。
3. **失败要响。** "猜一下、降级一下、静默返回空结果"这类写法一律不收。
   拿不到准确信息就报错，这个项目的全部价值就在这一点上。

## 加一个新厂商

只需要改 `model_presets.py` 里的 `FAMILIES`，加一条：

```python
_family("VendorName", ("model-prefix-*",),
        context=MAX, reasoning=SUMMARIZE, media=NATIVE,
        modes=("text", "image"),
        note="为什么这个厂商要这样处理")
```

顺序有意义：越具体的匹配越要靠前，第一个命中的生效。
加完请顺手补一条 `test_model_presets.py` 的用例。

## 改安装器

`install.py` 和 `configure.py` 会碰用户的 `~/.codex/config.toml`，所以：

- 只动 `base_url` 一个字段，其它一律不碰；
- 每个失败都要有固定的、不含密钥的错误码；
- 新增行为要有 `test_install.py` / `test_configure.py` 覆盖。

## 提交

- commit message 中英文都可以，重点是说清楚"为什么"。
- PR 描述里讲明白：改了什么、为什么改、怎么验证的。
- 不接受：遥测上报、自动上传用户数据、把密钥写进日志、绕过密钥扫描。

---

## English

Pull requests are welcome in English or Chinese. Run the offline suite first
(`python3 -m unittest discover -s . -p 'test_*.py'`), keep tests network-free,
never commit a credential, and prefer failing loudly over guessing. To teach
Code Ultra about a new vendor, extend `FAMILIES` in `model_presets.py` and add
a case to `test_model_presets.py`.
