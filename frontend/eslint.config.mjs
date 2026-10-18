import { defineConfig, globalIgnores } from 'eslint/config';
import js from '@eslint/js';
import parser from '@typescript-eslint/parser';
import typescript from '@typescript-eslint/eslint-plugin';
import hooks from 'eslint-plugin-react-hooks';
export default defineConfig([
  globalIgnores(['node_modules/**', 'dist/**', 'work/**', 'outputs/**']),
  { files: ['**/*.{ts,tsx}'], languageOptions: { parser, parserOptions: { ecmaFeatures: { jsx: true }, sourceType: 'module' } },
    plugins: { '@typescript-eslint': typescript, 'react-hooks': hooks },
    rules: { ...js.configs.recommended.rules, ...typescript.configs.recommended.rules,
      'no-undef': 'off', 'react-hooks/rules-of-hooks': 'error', 'react-hooks/exhaustive-deps': 'warn' } },
]);
