// src/utils/axiosInstance.js
import axios from 'axios';
import router from '@/router'; // Make sure router is imported to handle redirects

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

const axiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: 15000, // 15 seconds timeout
  headers: {
    'Accept': 'application/json',
  },
});

// Automatically attach token to requests
axiosInstance.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
}, (error) => {
  return Promise.reject(error);
});

// Handle expired or unauthorized token globally
axiosInstance.interceptors.response.use((response) => {
  return response;
}, (error) => {
  if (error.response && error.response.status === 401) {
    localStorage.removeItem('access_token');
    localStorage.removeItem('user_role');
    localStorage.removeItem('user_name');
    localStorage.removeItem('user_lang');
    router.push('/login'); // Redirect to login automatically
  }
  return Promise.reject(error);
});

export default axiosInstance;
