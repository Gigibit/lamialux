import { Sequelize } from 'sequelize';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const sequelize = new Sequelize({
  dialect: 'sqlite',
  storage: path.join(__dirname, 'dorapy.db'),
  logging: false
});

// Sincronizzazione dei modelli centralizzata
const syncDatabase = async () => {
  try {
    await sequelize.sync();
    console.log('✅ Database sincronizzato');
  } catch (err) {
    console.error('❌ Errore sincronizzazione DB:', err);
  }
};

export { sequelize, syncDatabase };
export default sequelize;
