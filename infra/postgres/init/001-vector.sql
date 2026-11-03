-- 只在数据卷为空（首次初始化）时由 postgres 镜像执行
CREATE EXTENSION IF NOT EXISTS vector;
