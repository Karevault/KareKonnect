-- Add reason column to telehealth_sessions table
ALTER TABLE telehealth_sessions ADD COLUMN reason TEXT;

-- Add reason column to telehealth_patient_flows table  
ALTER TABLE telehealth_patient_flows ADD COLUMN reason TEXT;
