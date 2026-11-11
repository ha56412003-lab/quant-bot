# strategies.yaml 历史备份存档

2026-11-11 从服务器 `/opt/quantbot/` 归档。**服务器上只留了 2 个**，
其余 5 个移到这里，避免服务器目录越来越乱（7 个 52K → 2 个 16K）。

## 保留在服务器上的（2 个）

| 文件 | md5 前缀 | 行数 | 为什么留 |
|---|---|---|---|
| `strategies.yaml.bak_1111_size50` | `a813ce3b` | 213 | 池子改 50 之前的**带完整注释**版本。
回滚参数、找回注释都靠它。最有用的一份。 |
| `strategies.yaml.bak_1111_nocomment` | `85e8b8c8` | 125 | 2026-11-11 20:09 审计脚本把注释洗掉后的
现场快照。参数值与现网 100% 一致，只是没注释 —— 用来对照「值是
不是被改过」很方便，也是那次事故的证据。 |

## 归档在这里的（5 个，服务器上已删）

| 文件 | md5 前缀 | 行数 | 是什么 | 为什么不留在服务器 |
|---|---|---|---|---|
| `strategies.yaml.bak_20261109` | `c69f2693` | 105 | 11-09 的早期版本 | 出场规则已重构两轮，回滚无意义 |
| `strategies.yaml.bak_before_exit_v2` | `18abbda4` | 109 | 出场 v2 重构之前 | 出场 v2 已上线并定档，旧版不再兼容 |
| `strategies.yaml.bak_1111` | `d75782d6` | 146 | 11-09 22:16 的版本 | 同上，已过两代 |
| `strategies.yaml.bak_before_mirror_bbw` | `e6c83ca2` | 136 | 加「镜像出场 + BBW」改动之前 | **镜像出场已物理删除**，BBW 已定档 70，此版本两个特性都作废 |
| `strategies.yaml.bak_1111b` | `a813ce3b` | 213 | **与 `bak_1111_size50` 字节完全相同** | 纯重复文件，删掉不损失任何信息 |

## 怎么用这些文件

```bash
# 看两个版本参数差异（只看值，不看注释）
cd /opt/quantbot
python3 -c "
import yaml,io
a=yaml.safe_load(io.open('strategies.yaml.bak_1111_size50',encoding='utf-8'))
b=yaml.safe_load(io.open('strategies.yaml',encoding='utf-8'))
print('完全相等' if a==b else '有差异')
"

# 回滚到某个备份（★ 先备份当前版本！）
cp -a strategies.yaml strategies.yaml.bak_before_rollback
cp -a strategies.yaml.bak_1111_size50 strategies.yaml
md5sum strategies.yaml && wc -l < strategies.yaml
systemctl restart quantbot      # yaml 本身热加载，但回滚时重启更保险
```

## 铁律

任何会触发 `save_strategies()` 的操作（网页改参数、审计脚本做「合法写入」往返、
本地起服务时的 migration）都会让 **PyYAML `dump()` 把注释全部丢掉**：
218 行 / 93 条注释 → 125 行 / 0 条注释，参数值不变。

所以做写入测试前必须先 `cp -a strategies.yaml /tmp/yaml_guard.yaml`，
测完 `cp -a` 回来并核对 md5 + 行数 + 注释数。
