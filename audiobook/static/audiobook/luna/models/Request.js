import { DataTypes } from 'sequelize';
import sequelize from '../db.js';

const Request = sequelize.define('Request', {
  ip: {
    type: DataTypes.STRING,
    allowNull: false,
  },
  count: {
    type: DataTypes.INTEGER,
    defaultValue: 1,
  },
});

export default Request;
