from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    try:
        conn.execute(text('ALTER TABLE telehealth_sessions ADD COLUMN reason TEXT'))
        print('Added reason column to telehealth_sessions')
    except Exception as e:
        print(f'telehealth_sessions: {e}')
    
    try:
        conn.execute(text('ALTER TABLE telehealth_patient_flows ADD COLUMN reason TEXT'))
        print('Added reason column to telehealth_patient_flows')
    except Exception as e:
        print(f'telehealth_patient_flows: {e}')
    
    conn.commit()
    print('Migration completed')
