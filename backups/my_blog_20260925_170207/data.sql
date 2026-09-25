-- ========================================================================
-- data.sql  |  database: my_blog
-- generated: 2026-09-25 17:02:07 local
-- Restore with:  mysql --default-character-set=utf8mb4 < data.sql
-- Run schema.sql first.
-- ========================================================================

SET NAMES utf8mb4;
SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0;
SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO';
SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0;

-- table blog: 12 row(s) (DB COUNT(*)=12)
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (1, '人工智能', 'LangGraph实战', '教程', ',LangGraph,Agent,状态机,', 203, 1, 1, 'LangGraph 子图完全指南：从 StateGraph 到嵌套编译', '阿泽', '用一个可运行的例子讲透子图的状态共享、私有状态与中断恢复。', 'https://blog.example.com/langgraph-subgraph', '2025-03-11', 15800, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (2, '人工智能', 'RAG实战', '实战', ',RAG,向量检索,召回率,', 4800, 1, 2, 'RAG 检索质量调优的 7 个实战技巧', '林深', '从切分粒度到重排序，逐项拆解召回率提升的工程手段。', 'https://blog.example.com/rag-tuning', '2025-04-02', 23400, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (3, '人工智能', 'Agent设计', '原理剖析', ',Agent,记忆,Store,', 6400, 0, 3, '大模型 Agent 的记忆设计：短期、长期与画像', '阿泽', '把用户偏好沉淀成长期记忆，让助手越用越懂你。', 'https://blog.example.com/agent-memory', '2025-04-15', 20900, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (4, '数据库', 'Text2SQL', '踩坑记录', ',Text2SQL,SQL,LLM,', 2600, 1, 2, 'Text2SQL 在真实业务库上的踩坑记录', '阿泽', '方言差异、字段幻觉、JOIN 失控——一次线上事故的完整复盘。', 'https://blog.example.com/text2sql-pitfalls', '2025-02-18', 9800, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (5, '数据库', 'SQL调优', '原理剖析', ',MySQL,索引,执行计划,', 3900, 1, 2, 'MySQL 索引失效的 12 种典型场景', '陈默', '结合 EXPLAIN 输出，逐个还原索引失效的真实原因。', 'https://blog.example.com/mysql-index', '2025-03-05', 33100, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (6, '前端开发', '性能优化', '教程', ',前端,性能,懒加载,', 5600, 1, 2, '写给后端工程师的前端性能优化清单', '小满', '首屏、交互、渲染三条链路，按优先级给出可落地的优化项。', 'https://blog.example.com/fe-perf', '2025-01-25', 18700, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (7, '后端开发', '语言进阶', '教程', ',Python,asyncio,并发,', 7200, 1, 1, 'Python 异步编程从入门到放弃（再入门）', '林深', '事件循环、协程调度与常见阻塞陷阱，配 12 段可运行代码。', 'https://blog.example.com/python-async', '2025-01-09', 27600, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (8, '运维部署', '工程效率', '实战', ',Docker,镜像,CI,', 2200, 1, 2, 'Docker 镜像瘦身：从 1.2G 到 180M', '陈默', '多阶段构建、基础镜像选择与层缓存的正确打开方式。', 'https://blog.example.com/docker-slim', '2025-02-06', 14200, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (9, '职场成长', '每周随笔', '随笔', ',写作,增长,复盘,', 1800, 0, 1, '我用一个月把个人博客访问量做到 10 万', '周周', '选题、分发、复用——一个普通开发者的内容增长复盘。', 'https://blog.example.com/blog-growth', '2025-03-30', 31200, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (10, '职场成长', '每周随笔', '随笔', ',读书,方法论,习惯,', 1500, 0, 1, '我的 2025 年度书单与读书方法', '周周', '一年 42 本书，说说怎么选、怎么读、怎么记住。', 'https://blog.example.com/reading-2025', '2025-04-20', 8600, 1);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (11, '人工智能', 'LangGraph实战', '踩坑记录', ',LangGraph,部署,', 2000, 1, 3, '【草稿】LangGraph 部署到生产环境的坑', '阿泽', '这篇还没写完，不应该被推荐出来。', 'https://blog.example.com/langgraph-deploy', '2025-04-25', 0, 0);
INSERT INTO `blog` (`id`, `topic`, `category`, `blog_type`, `tags`, `word_count`, `has_code`, `difficulty`, `title`, `author`, `intro`, `blog_url`, `publish_date`, `view_count`, `status`) VALUES (12, '前端开发', '框架测评', '工具介绍', ',过时,测评,', 1200, 0, 1, '【已下架】某个过时的框架测评', '陈默', '内容已过时下架，不应该被推荐出来。', 'https://blog.example.com/old-framework', '2024-08-01', 500, -1);

-- table blog_content: 1 row(s) (DB COUNT(*)=1)
INSERT INTO `blog_content` (`blog_id`, `content`, `content_fmt`, `toc`, `code_lang`, `content_hash`, `updated_at`) VALUES (1, '# LangGraph 子图完全指南\n\n子图就是把一个编译好的图当作节点塞进另一个图，子图能独立编译、独立调试，也能把自己的私有状态带进主图。\n\n```python\nbuilder.add_node(\"recommended_graph\", recommended_graph)\n```\n\n## 一、为什么需要子图\n\n把一个大图拆成几个小图，每个小图有自己的 State，只在需要的时候把共享字段往外传。\n', 'markdown', NULL, 'python', NULL, '2026-09-21 18:41:25');

SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS;
SET SQL_MODE=@OLD_SQL_MODE;
SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS;
