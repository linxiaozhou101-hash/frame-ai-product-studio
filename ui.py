"""Small presentation helpers; all controls remain native Streamlit widgets."""

import streamlit as st


def apply_styles() -> None:
    st.markdown(
        """<style>
        .block-container { max-width: 1160px; padding-top: 2.8rem; padding-bottom: 2rem; }
        h1 { font-size: 2.35rem !important; letter-spacing: -.055em; font-weight: 650 !important; }
        h3 { font-size: 1.08rem !important; font-weight: 600 !important; }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 18px; }
        [data-testid="stFileUploader"] section { background: #f0f3ed; border-radius: 12px; }
        [data-testid="stButton"] button, [data-testid="stDownloadButton"] button { border-radius: 10px; min-height: 44px; }
        [data-testid="stImage"] img { max-height: 460px; object-fit: contain; border-radius: 12px; }
        .brand { font-size: 12px; letter-spacing: .18em; color: #527466; font-weight: 650; margin-bottom: 16px; }
        .subtitle { color: #718078; font-size: 15px; margin: -6px 0 26px; }
        .empty-image { height: 320px; border: 1px dashed #d4ddd2; border-radius: 14px;
                       background: linear-gradient(145deg, #f2f4ee, #e7ede4); display: flex;
                       flex-direction: column; align-items: center; justify-content: center;
                       color: #6b7c70; text-align: center; }
        .empty-image .frame { font-size: 44px; color: #8ba08d; margin-bottom: 18px; font-weight: 200; }
        .empty-image strong { color: #526759; font-size: 16px; font-weight: 500; }
        .empty-image small { margin-top: 8px; font-size: 13px; }
        .footnote { border-top: 1px solid #e1e7dd; margin-top: 26px; padding-top: 16px;
                    color: #859087; font-size: 12px; letter-spacing: .02em; }
        @media (max-width: 640px) { .block-container { padding-top: 1.5rem; } h1 { font-size: 1.9rem !important; } }
        </style>""",
        unsafe_allow_html=True,
    )


def empty_image(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="empty-image"><div class="frame">▧</div><strong>{title}</strong><small>{subtitle}</small></div>',
        unsafe_allow_html=True,
    )
