-- 既存のテーブルがあれば削除
DROP TABLE IF EXISTS sales_records;
DROP TABLE IF EXISTS contacts;
DROP TABLE IF EXISTS companies;

-- 1. 企業テーブル (基本情報)
CREATE TABLE companies (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    industry VARCHAR(100),
    region VARCHAR(50),
    established_year INT
);

-- 2. 担当者テーブル (連絡先情報)
CREATE TABLE contacts (
    id SERIAL PRIMARY KEY,
    company_id INT REFERENCES companies(id),
    name VARCHAR(100),
    position VARCHAR(100),
    email VARCHAR(255)
);

-- 3. 商談・売上テーブル (動的なトランザクションデータ)
CREATE TABLE sales_records (
    id SERIAL PRIMARY KEY,
    company_id INT REFERENCES companies(id),
    status VARCHAR(50), -- '商談中', '契約済', '失注', 'リード'
    sales_amount INT, -- 金額
    last_contact_date DATE,
    product_category VARCHAR(100)
);

-- ==========================================
-- データ挿入 (10件以上の多様なデータ)
-- ==========================================

-- A. 企業データの挿入
INSERT INTO companies (name, industry, region, established_year) VALUES
('株式会社A-Tech', '電子材料', '上海', 2005),
('B自動車工業', '自動車部品', '広州', 1998),
('C未来通信', 'IT通信', '北京', 2012),
('D精密ガラス', 'ガラス製造', '深セン', 2001),
('E-Energy', '新エネルギー', '上海', 2018),
('F物流ソリューション', '物流・倉庫', '天津', 2010),
('G建機レンタル', '建設機械', '武漢', 2008),
('H半導体デザイン', '半導体', '蘇州', 2020),
('I食品加工', '食品', '青島', 1995),
('Jメディカル', '医療機器', '成都', 2015);

-- B. 担当者データの挿入
INSERT INTO contacts (company_id, name, position, email) VALUES
(1, '伊藤', '購買部長', 'ito@atech.cn'),
(2, '田中', '工場長', 'tanaka@b-auto.cn'),
(3, '佐藤', 'CTO', 'sato@c-future.cn'),
(4, '鈴木', '営業マネージャー', 'suzuki@d-glass.cn'),
(5, '王', 'プロジェクトMgr', 'wang@energy.cn'),
(6, '李', '物流センター長', 'li@f-logi.cn'),
(7, '張', '調達担当', 'zhang@g-kenki.cn'),
(8, '陳', '設計主任', 'chen@h-semi.cn'),
(9, '劉', '工場長代理', 'liu@i-food.cn'),
(10, '楊', '院長秘書', 'yang@j-med.cn');

-- C. 商談データの挿入 (ステータスや金額をばらけさせる)
INSERT INTO sales_records (company_id, status, sales_amount, last_contact_date, product_category) VALUES
(1, '商談中', 5000, '2024-02-01', '高性能フィルム'),
(2, '契約済', 12000, '2023-12-15', '強化ガラス'),
(3, '失注', 0, '2023-11-20', '光ファイバー'),
(4, '商談中', 3500, '2024-01-10', '研磨剤'),
(5, 'リード', 0, '2024-02-10', 'バッテリー部材'),
(6, '契約済', 8000, '2024-01-25', '自動搬送ロボット'),
(7, '商談中', 20000, '2024-02-05', '油圧部品'),
(8, '商談中', 15000, '2024-02-12', 'EDAツール'),
(9, '失注', 0, '2023-10-05', '包装機械'),
(10, '契約済', 6500, '2024-01-15', 'MRI部品');