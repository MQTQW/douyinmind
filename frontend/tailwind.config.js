/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        warm: {
          bg: '#F4F7EF',
          panel: '#FFFEFA',
          50: '#F8FAF5',
          100: '#EAF1E3',
          200: '#DCE5D5',
        },
        accent: {
          DEFAULT: '#3E6747',
          hover: '#31583B',
          light: '#E2EDDA',
        },
        amber: {
          DEFAULT: '#96682D',
          light: '#F7F0DF',
        },
        ink: {
          DEFAULT: '#273A2A',
          soft: '#4D5E4E',
          muted: '#647360',
        },
        success: {
          DEFAULT: '#3E6747',
          light: '#E2EDDA',
        },
      },
      fontFamily: {
        body: ['"Noto Sans SC"', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif'],
        display: ['"Noto Sans SC"', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
