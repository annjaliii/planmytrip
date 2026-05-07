USE Planmytrip;

CREATE TABLE feedbacks (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_email  VARCHAR(150)  NOT NULL,             
    city        VARCHAR(100)  NOT NULL,             
    rating      TINYINT       NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment     TEXT,
    created_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
);

