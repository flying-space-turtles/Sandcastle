import React from 'react';
import { createRoot } from 'react-dom/client';
import 'reactflow/dist/style.css';
import './styles.css';
import App from './App.tsx';

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
