-- ========================================================================
-- schema.sql  |  database: my_blog
-- generated: 2026-09-25 17:07:20 local
-- server   : MySQL 8.0.45-0ubuntu0.24.04.1
-- tool     : scripts/backup_my_blog.py (read-only SHOW/SELECT export)
--
-- Restore order: tables (dependency sorted) -> FK constraints -> views.
-- No DROP is emitted for tables; re-create into an empty schema.
-- ========================================================================

SET NAMES utf8mb4;
SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0;
SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO';
SET @OLD_TIME_ZONE=@@TIME_ZONE, TIME_ZONE='+00:00';

-- ---------------------------------------------------------------
-- table: blog
-- ---------------------------------------------------------------
DROP TABLE IF EXISTS `blog`;
CREATE TABLE `blog` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '文章ID',
  `topic` varchar(64) NOT NULL COMMENT '领域/分类',
  `category` varchar(64) DEFAULT NULL COMMENT '栏目/专栏',
  `blog_type` varchar(32) DEFAULT NULL COMMENT '文章类型',
  `tags` varchar(255) DEFAULT NULL COMMENT '标签，逗号分隔，形如 ,a,b,',
  `word_count` int unsigned DEFAULT NULL COMMENT '散文字数（由正文触发）',
  `has_code` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否带代码示例',
  `difficulty` tinyint NOT NULL DEFAULT '2' COMMENT '难度 1入门 2进阶 3深入',
  `title` varchar(255) NOT NULL COMMENT '标题',
  `author` varchar(64) NOT NULL COMMENT '作者',
  `intro` varchar(500) DEFAULT NULL COMMENT '摘要',
  `blog_url` varchar(512) DEFAULT NULL COMMENT '文章链接',
  `publish_date` date DEFAULT NULL COMMENT '发布日期',
  `view_count` int unsigned NOT NULL DEFAULT '0' COMMENT '阅读量，用于排序',
  `status` tinyint NOT NULL DEFAULT '1' COMMENT '1已发布 0草稿 -1下架',
  PRIMARY KEY (`id`),
  KEY `idx_topic_word` (`topic`,`word_count`),
  KEY `idx_topic_hot` (`topic`,`view_count`),
  KEY `idx_publish` (`publish_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='博客文章表';

-- ---------------------------------------------------------------
-- table: blog_content
-- ---------------------------------------------------------------
DROP TABLE IF EXISTS `blog_content`;
CREATE TABLE `blog_content` (
  `blog_id` bigint unsigned NOT NULL,
  `content` longtext COMMENT '正文 Markdown',
  `content_fmt` varchar(16) NOT NULL DEFAULT 'markdown',
  `toc` json DEFAULT NULL,
  `code_lang` varchar(64) DEFAULT NULL,
  `content_hash` char(64) DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`blog_id`),
  FULLTEXT KEY `ft_content` (`content`) /*!50100 WITH PARSER `ngram` */
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='博客正文表';

-- ---------------------------------------------------------------
-- foreign key constraints (deferred until every table exists)
-- These were removed from the CREATE TABLE bodies above;
-- this is the single authoritative definition of each FK.
-- ---------------------------------------------------------------
ALTER TABLE `blog_content`
  ADD CONSTRAINT `fk_content_blog`
  FOREIGN KEY (`blog_id`)
  REFERENCES `blog` (`id`) ON DELETE CASCADE ON UPDATE NO ACTION;

-- ---------------------------------------------------------------
-- view: v_blog
-- ---------------------------------------------------------------
DROP VIEW IF EXISTS `v_blog`;
CREATE ALGORITHM=MERGE SQL SECURITY INVOKER VIEW `v_blog` AS select `blog`.`id` AS `id`,`blog`.`title` AS `title`,`blog`.`author` AS `author`,`blog`.`topic` AS `topic`,`blog`.`category` AS `category`,`blog`.`blog_type` AS `blog_type`,`blog`.`tags` AS `tags`,`blog`.`word_count` AS `word_count`,`blog`.`difficulty` AS `difficulty`,`blog`.`has_code` AS `has_code`,`blog`.`intro` AS `intro`,`blog`.`blog_url` AS `blog_url`,`blog`.`view_count` AS `view_count`,`blog`.`publish_date` AS `publish_date` from `blog` where (`blog`.`status` = 1);

SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS;
SET SQL_MODE=@OLD_SQL_MODE;
SET TIME_ZONE=@OLD_TIME_ZONE;
