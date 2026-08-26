import type { NextConfig } from "next";

/**
 * Setting API_URL makes Next proxy /api/* to the FastAPI engine: in compose it
 * is http://api:8000, locally http://127.0.0.1:8000. Leaving it unset disables
 * the rewrite, which is what a platform that routes /api itself (Vercel's
 * Python function, per vercel.json) needs.
 */
const API_URL = process.env.API_URL;

const nextConfig: NextConfig = {
  // Standalone output keeps the runtime image small for the VPS deploy.
  output: "standalone",
  // No next/image anywhere in this app, and skipping the optimizer means the
  // runtime image needs no platform-specific sharp binary. That is what lets
  // the standalone bundle be built natively and copied into a linux/amd64
  // image, instead of emulating an entire Next build under QEMU.
  images: { unoptimized: true },
  async rewrites() {
    if (!API_URL) return [];
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};

export default nextConfig;
