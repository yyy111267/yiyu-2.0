# 数据库迁移（Alembic）

MVP 阶段用 SQLite，可先不建迁移，直接 `Base.metadata.create_all` 建表。

**什么时候需要这里**：当你把数据库换成 PostgreSQL、且线上已有数据、又要改表结构时，
用 Alembic 记录每次结构变化，安全升级而不丢数据。

初始化命令（真正要用时再跑）：
```
alembic init migrations
alembic revision --autogenerate -m "init tables"
alembic upgrade head
```
