/** @type {import('next').NextConfig} */
module.exports = {
  reactStrictMode: true,
  // Hide Next.js's dev-only "static route" badge (the lightning bolt that
  // floats in the bottom-left during `npm run dev`). It overlapped the
  // sidebar "Sign out" button. Dev-only — production never showed it.
  devIndicators: {
    appIsrStatus: false,
  },
  eslint: {
    ignoreDuringBuilds: true, // lint runs as its own CI step; don't block builds
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://localhost:8000"}/api/:path*`,
      },
    ];
  },
};
