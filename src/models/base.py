# src/models/base.py

# 数据库模型基类
# 这个文件定义了所有数据库模型都必须继承的 SQLAlchemy 声明式基类。
# 通过这种方式，SQLAlchemy 的元数据功能可以统一管理所有的表和映射。
from sqlalchemy.orm import declarative_base

# 创建一个所有模型共享的基类实例。
# 项目中所有的 ORM 模型（如 Rule, Group 等）都将继承自这个 Base 类。
Base = declarative_base()
