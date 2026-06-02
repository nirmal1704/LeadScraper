import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Add allowedDevOrigins if experimental or custom server logic requires it,
  // but typically it's not a standard NextConfig property. We'll add it safely.
  experimental: {
    serverActions: {
      allowedOrigins: ['10.218.102.147', 'localhost:3000'],
    },
  },
};

export default nextConfig;
