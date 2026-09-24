import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export", // static export for Firebase Hosting
  images: {
    unoptimized: true,
  },
  trailingSlash: true,
};

export default nextConfig;
