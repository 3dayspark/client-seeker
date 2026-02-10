CREATE EXTENSION IF NOT EXISTS vector;

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
    established_year INT,
    search_embedding vector(1024) 
);

-- 2. 担当者テーブル (連絡先情報)
CREATE TABLE contacts (
    id SERIAL PRIMARY KEY,
    company_id INT REFERENCES companies(id),
    name VARCHAR(100),
    position VARCHAR(100),
    email VARCHAR(255),
    search_embedding vector(1024) 
);

-- 3. 商談・売上テーブル (動的なトランザクションデータ)
CREATE TABLE sales_records (
    id SERIAL PRIMARY KEY,
    company_id INT REFERENCES companies(id),
    status VARCHAR(50), -- '商談中', '契約済', '失注', 'リード'
    sales_amount INT, -- 金額
    last_contact_date DATE,
    product_category VARCHAR(100),
    search_embedding vector(1024) 
);

-- ==========================================
-- データ挿入 (10件以上の多様なデータ)
-- ==========================================

-- A. 企業データ（実在感のある中国企業名）
INSERT INTO companies (name, industry, region, established_year) VALUES
('上海華信電子材料有限公司', '電子材料・部材', '中国・上海市', 2005),
('広州天成汽車部品有限公司', '自動車部品製造', '中国・広州市', 1998),
('北京未来通信科技有限公司', '情報通信・ネットワーク', '中国・北京市', 2012),
('深圳精工ガラス製造有限公司', '工業用ガラス製造', '中国・深セン', 2001),
('上海新源エナジー科技有限公司', '再生可能エネルギー', '中国・上海市', 2018),
('天津東方物流サービス有限公司', '物流・倉庫運営', '中国・天津市', 2010),
('武漢中建機械リース有限公司', '建設機械レンタル', '中国・武漢市', 2008),
('蘇州芯創半導体設計有限公司', '半導体設計', '中国・蘇州市', 2020),
('青島海豊食品加工有限公司', '食品加工・製造', '中国・青島市', 1995),
('成都安信医療機器有限公司', '医療機器製造', '中国・成都市', 2015),
('深セン水素テクノロジー', '水素燃料電池', '中国・深セン', 2019),  
('FCEV Motor水素自動車工業', '水素自動車', '中国・広州市', 2016), 
('仏山環境発電システム', '発電設備', '中国・仏山', 2021); 


-- B. 担当者データ（日本企業CRM標準：姓＋役職）
INSERT INTO contacts (company_id, name, position, email) VALUES
(1, '伊藤', '購買部 部長', 'ito@huaxin-materials.cn'),
(2, '田中', '生産工場長', 'tanaka@tc-autoparts.cn'),
(3, '佐藤', '技術責任者（CTO）', 'sato@future-comm.cn'),
(4, '鈴木', '営業部 マネージャー', 'suzuki@jingonglass.cn'),
(5, '王', 'プロジェクトマネージャー', 'wang@xinyuan-energy.cn'),
(6, '李', '物流センター 統括責任者', 'li@df-logistics.cn'),
(7, '張', '調達担当 主任', 'zhang@zj-machinery.cn'),
(8, '陳', '設計部 主任エンジニア', 'chen@coresemi.cn'),
(9, '劉', '製造工場 副工場長', 'liu@haifeng-food.cn'),
(10, '楊', '院長秘書室', 'yang@anxin-medical.cn'),
(11, '趙', 'R&D部長', 'zhao@k-hydrogen.cn'),
(12, '孫', '調達マネージャー', 'sun@l-fcev.cn'),
(13, '周', 'CEO', 'zhou@m-green.cn');


-- C. 商談データ（日本円・現実的レンジ・状態分布）
INSERT INTO sales_records (company_id, status, sales_amount, last_contact_date, product_category) VALUES
(1, '商談中', 48000000, '2024-02-01', '高機能フィルム材料'),
(2, '契約済', 125000000, '2023-12-15', '自動車用強化ガラス'),
(3, '失注', 0, '2023-11-20', '光通信ケーブル'),
(4, '商談中', 36000000, '2024-01-10', '精密研磨材'),
(5, 'リード', 0, '2024-02-10', '蓄電池用材料'),
(6, '契約済', 89000000, '2024-01-25', '自動搬送ロボット'),
(7, '商談中', 210000000, '2024-02-05', '建設機械用油圧部品'),
(8, '商談中', 158000000, '2024-02-12', '半導体EDAソフトウェア'),
(9, '失注', 0, '2023-10-05', '食品包装機械'),
(10, '契約済', 72000000, '2024-01-15', 'MRI装置用精密部品'),
(11, '商談中', 18000000, '2024-02-14', '燃料電池スタック用触媒'),
(12, 'リード', 0, '2024-02-15', '高圧水素タンク'),
(13, '契約済', 45000000, '2024-01-20', '定置用燃料電池システム'); 