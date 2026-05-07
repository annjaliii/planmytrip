CREATE DATABASE Planmytrip
CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE Planmytrip;

CREATE TABLE users (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(100)  NOT NULL,
    email      VARCHAR(150)  NOT NULL UNIQUE,
    phone      VARCHAR(20)   NOT NULL,             
    password   VARCHAR(255)  NOT NULL,              
    created_at TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
);