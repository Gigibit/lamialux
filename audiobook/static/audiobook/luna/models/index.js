import sequelize from '../db.js';
import Request from './Request.js';

const db = {
  sequelize,
  Request,
};

await db.sequelize.sync({ alter: true });
console.log('Database sincronizzato!');

export default db;
