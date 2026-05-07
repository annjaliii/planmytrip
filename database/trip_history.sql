USE Planmytrip;

CREATE TABLE trip_history (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_email  VARCHAR(150)  NOT NULL,            
    city        VARCHAR(100)  NOT NULL,             
    days        INT           DEFAULT 1,
    budget      INT           DEFAULT 0,
    people      INT           DEFAULT 1,            
    budget_type VARCHAR(20)   DEFAULT 'medium',     
    per_person  INT           DEFAULT 0,            
    created_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
);