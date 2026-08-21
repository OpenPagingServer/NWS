CREATE TABLE IF NOT EXISTS `endpoints-input-nws` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(255) NOT NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `groups` TEXT DEFAULT NULL,
  `entries_json` LONGTEXT DEFAULT NULL,
  `last_checked` DATETIME DEFAULT NULL,
  `last_error` TEXT DEFAULT NULL,
  PRIMARY KEY (`id`)
);

CREATE TABLE IF NOT EXISTS `endpoints-input-nws-active` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `endpoint_id` INT NOT NULL,
  `entry_id` VARCHAR(64) NOT NULL,
  `alert_id` VARCHAR(255) NOT NULL,
  `broadcast_id` VARCHAR(64) DEFAULT NULL,
  `last_seen` DATETIME DEFAULT NULL,
  `expires_at` DATETIME DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_nws_endpoint_entry_alert` (`endpoint_id`,`entry_id`,`alert_id`)
);

CREATE TABLE IF NOT EXISTS `endpoints-input-nws-drafts` (
  `token` VARCHAR(128) NOT NULL,
  `data` LONGTEXT NOT NULL,
  `updated_at` DATETIME NOT NULL,
  PRIMARY KEY (`token`)
);
