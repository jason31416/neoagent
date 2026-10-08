"""QQ 通道：NapCat / OneBot 11 WebSocket 适配器。

协议要点（踩坑记录，改协议层前先看）：
- NapCat 起 **WebSocket 服务器**，neoagent 是 client（ws://host:port 根路径），
  token 走 Authorization: Bearer <token>；断线自动退避重连；
- API 与事件混在同一连接：调用发 {"action","params","echo"}，回包按 echo 对号；
- 收消息：post_type=message，message 字段是消息段数组或 CQ 码字符串，两种都认；
  群聊里 @机器人 是 at 段（qq == self_id），判定是精确的，不用降级；
- 回消息：send_group_msg / send_private_msg，message 传消息段数组，
  引用 = {"type":"reply","data":{"id":消息id}}；发图/文件用 base64:// 传字节
  （napcat 和我们不一定同一台机器，本地路径不可靠）。
"""
