--
-- PostgreSQL database dump
--

\restrict u2tFnv44vt4SBlnS5xztGcGDYczH3rmqRP5A7rK5kE8awIbL1ZbvSQsiIrIwI2E

-- Dumped from database version 16.14
-- Dumped by pg_dump version 16.14

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: accounttype; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.accounttype AS ENUM (
    'SAVINGS',
    'CURRENT'
);


ALTER TYPE public.accounttype OWNER TO postgres;

--
-- Name: risklevel; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.risklevel AS ENUM (
    'LOW',
    'MEDIUM',
    'HIGH'
);


ALTER TYPE public.risklevel OWNER TO postgres;

--
-- Name: supportsender; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.supportsender AS ENUM (
    'MERCHANT',
    'SUPPORT'
);


ALTER TYPE public.supportsender OWNER TO postgres;

--
-- Name: txstatus; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.txstatus AS ENUM (
    'PENDING',
    'ADMIN_APPROVED',
    'COMPLETED',
    'SUCCESSFUL',
    'REJECTED',
    'SA_REJECTED',
    'CANCELLED',
    'ACCOUNT_REQUESTED',
    'ACCOUNT_SUBMITTED'
);


ALTER TYPE public.txstatus OWNER TO postgres;

--
-- Name: txtype; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.txtype AS ENUM (
    'DEPOSIT',
    'WITHDRAWAL',
    'SETTLEMENT',
    'DEPOSIT_REQUEST',
    'WITHDRAWAL_REQUEST',
    'SETTLEMENT_REQUEST'
);


ALTER TYPE public.txtype OWNER TO postgres;

--
-- Name: userrole; Type: TYPE; Schema: public; Owner: postgres
--

CREATE TYPE public.userrole AS ENUM (
    'SUPER_ADMIN',
    'ADMIN',
    'MERCHANT',
    'SUPPORT_AGENT'
);


ALTER TYPE public.userrole OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: account_master; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.account_master (
    id integer NOT NULL,
    reference_number character varying(40) NOT NULL,
    account_name character varying(128) NOT NULL,
    account_number character varying(32) NOT NULL,
    ifsc_code character varying(16) NOT NULL,
    bank_name character varying(128) NOT NULL,
    branch character varying(128) NOT NULL,
    account_type public.accounttype NOT NULL,
    status character varying(24) NOT NULL,
    created_date date NOT NULL,
    created_time character varying(16) NOT NULL,
    last_maintenance_date date,
    last_maintenance_time character varying(16)
);


ALTER TABLE public.account_master OWNER TO postgres;

--
-- Name: account_master_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.account_master_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.account_master_id_seq OWNER TO postgres;

--
-- Name: account_master_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.account_master_id_seq OWNED BY public.account_master.id;


--
-- Name: account_transaction; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.account_transaction (
    id integer NOT NULL,
    reference_number character varying(40) NOT NULL,
    member_id character varying(64),
    transaction_reference_number character varying(32),
    transaction_date date NOT NULL,
    transaction_time character varying(16) NOT NULL
);


ALTER TABLE public.account_transaction OWNER TO postgres;

--
-- Name: account_transaction_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.account_transaction_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.account_transaction_id_seq OWNER TO postgres;

--
-- Name: account_transaction_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.account_transaction_id_seq OWNED BY public.account_transaction.id;


--
-- Name: support_messages; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.support_messages (
    id integer NOT NULL,
    merchant_id integer NOT NULL,
    sender public.supportsender NOT NULL,
    sender_name character varying(128) NOT NULL,
    content text NOT NULL,
    read boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.support_messages OWNER TO postgres;

--
-- Name: support_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.support_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.support_messages_id_seq OWNER TO postgres;

--
-- Name: support_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.support_messages_id_seq OWNED BY public.support_messages.id;


--
-- Name: transactions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.transactions (
    id integer NOT NULL,
    ref character varying(32) NOT NULL,
    type public.txtype NOT NULL,
    amount double precision NOT NULL,
    status public.txstatus NOT NULL,
    merchant_id integer NOT NULL,
    merchant_name character varying(128) NOT NULL,
    tx_date date NOT NULL,
    tx_time character varying(16) NOT NULL,
    deposit_type character varying(16),
    member_name character varying(128),
    member_id character varying(64),
    segment character varying(4),
    bank_name character varying(128),
    account_holder character varying(128),
    account_number character varying(32),
    ifsc character varying(16),
    merchant_proof text,
    admin_proof text,
    admin_ref character varying(64),
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.transactions OWNER TO postgres;

--
-- Name: transactions_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.transactions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.transactions_id_seq OWNER TO postgres;

--
-- Name: transactions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.transactions_id_seq OWNED BY public.transactions.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.users (
    id integer NOT NULL,
    username character varying(64) NOT NULL,
    hashed_password character varying(256) NOT NULL,
    role public.userrole NOT NULL,
    email character varying(128) NOT NULL,
    name character varying(128) NOT NULL,
    phone character varying(24),
    active boolean NOT NULL,
    created date NOT NULL,
    created_by integer,
    pay_in character varying(8),
    pay_out character varying(8),
    settlement character varying(8),
    pay_in_fee double precision,
    pay_out_fee double precision,
    balance double precision,
    risk public.risklevel,
    profile character varying(32)
);


ALTER TABLE public.users OWNER TO postgres;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.users_id_seq OWNER TO postgres;

--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: account_master id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.account_master ALTER COLUMN id SET DEFAULT nextval('public.account_master_id_seq'::regclass);


--
-- Name: account_transaction id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.account_transaction ALTER COLUMN id SET DEFAULT nextval('public.account_transaction_id_seq'::regclass);


--
-- Name: support_messages id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.support_messages ALTER COLUMN id SET DEFAULT nextval('public.support_messages_id_seq'::regclass);


--
-- Name: transactions id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.transactions ALTER COLUMN id SET DEFAULT nextval('public.transactions_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Data for Name: account_master; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.account_master (id, reference_number, account_name, account_number, ifsc_code, bank_name, branch, account_type, status, created_date, created_time, last_maintenance_date, last_maintenance_time) FROM stdin;
1	ACC0000001	Nexus Settlement A/C	50100100100100	HDFC0001234	HDFC Bank	MG Road, Bengaluru	CURRENT	ACTIVE	2025-06-02	10:19:59	2026-06-10	10:19:59
2	ACC0000002	BrightPay Payout A/C	50100200200200	ICIC0005678	ICICI Bank	Bandra, Mumbai	CURRENT	ACTIVE	2025-07-16	10:19:59	2026-06-09	10:19:59
3	ACC0000003	ZenPay Operating A/C	50100300300300	SBIN0009999	State Bank of India	Anna Salai, Chennai	SAVINGS	INACTIVE	2025-08-03	10:19:59	2026-06-01	10:19:59
\.


--
-- Data for Name: account_transaction; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.account_transaction (id, reference_number, member_id, transaction_reference_number, transaction_date, transaction_time) FROM stdin;
1	ACC0000001	MBR20240001	DEP0000001	2026-06-10	10:19:59
2	ACC0000002	MBR20240050	BST0000001	2026-06-09	10:19:59
\.


--
-- Data for Name: support_messages; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.support_messages (id, merchant_id, sender, sender_name, content, read, created_at) FROM stdin;
1	5	MERCHANT	Nexus Fintech Ltd.	Hi, my deposit DEP0000002 is still pending. Can you check?	t	2026-06-15 10:19:59.347273
2	5	SUPPORT	Sana Kapoor	Hello! Sure, let me take a look at DEP0000002 for you right away.	t	2026-06-15 10:19:59.347277
3	6	MERCHANT	BrightPay Inc.	How long does a settlement request usually take?	f	2026-06-15 10:19:59.347278
4	5	MERCHANT	Nexus Fintech Ltd.	hello	f	2026-06-15 10:23:39.122987
\.


--
-- Data for Name: transactions; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.transactions (id, ref, type, amount, status, merchant_id, merchant_name, tx_date, tx_time, deposit_type, member_name, member_id, segment, bank_name, account_holder, account_number, ifsc, merchant_proof, admin_proof, admin_ref, created_at) FROM stdin;
1	DEP0000001	DEPOSIT_REQUEST	125000	ACCOUNT_SUBMITTED	5	Nexus Fintech Ltd.	2026-06-10	09:14:32	NEFT	Raj Kumar	MBR20240001	\N	\N	\N	\N	\N	\N	\N	ADMREF-1001	2026-06-15 10:19:59.322448
2	BWI0000001	WITHDRAWAL_REQUEST	50000	ACCOUNT_REQUESTED	6	BrightPay Inc.	2026-06-11	11:02:18	\N	\N	MBR20240050	\N	HDFC Bank	BrightPay Inc.	50100123456789	HDFC0001234	\N	\N	\N	2026-06-15 10:19:59.322454
3	DEP0000002	DEPOSIT_REQUEST	75000	ACCOUNT_REQUESTED	5	Nexus Fintech Ltd.	2026-06-12	08:45:00	UPI	Anita Singh	MBR20240001	\N	\N	\N	\N	\N	\N	\N	\N	2026-06-15 10:19:59.322456
4	BST0000001	SETTLEMENT_REQUEST	200000	ACCOUNT_SUBMITTED	6	BrightPay Inc.	2026-06-09	15:30:00	\N	\N	MBR20240050	\N	\N	\N	\N	\N	\N	\N	ADMREF-1002	2026-06-15 10:19:59.322457
5	WIT0000002	WITHDRAWAL_REQUEST	30000	ACCOUNT_REQUESTED	5	Nexus Fintech Ltd.	2026-06-08	13:22:47	\N	\N	MBR20240002	\N	SBI	\N	\N	\N	\N	\N	\N	2026-06-15 10:19:59.322458
6	BDP0000001	DEPOSIT_REQUEST	95000	ACCOUNT_REQUESTED	6	BrightPay Inc.	2026-06-12	10:05:33	IMPS	Suresh Patel	MBR20240051	\N	\N	\N	\N	\N	\N	\N	\N	2026-06-15 10:19:59.322459
7	DEP0000003	DEPOSIT_REQUEST	60000	ACCOUNT_SUBMITTED	5	Nexus Fintech Ltd.	2026-06-07	12:00:00	UPI	Raj Kumar	MBR20240001	\N	\N	\N	\N	\N	\N	\N	ADMREF-1003	2026-06-15 10:19:59.32246
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.users (id, username, hashed_password, role, email, name, phone, active, created, created_by, pay_in, pay_out, settlement, pay_in_fee, pay_out_fee, balance, risk, profile) FROM stdin;
1	superadmin	$2b$12$kQ4v7.Thvj.x27ozq6a/M.V3eKw6Cg57FgI2PtHCeZqoCUY7EoG2y	SUPER_ADMIN	sa@clari5pay.io	Arjun Sharma	+91 98000 11111	t	2025-01-01	\N	\N	\N	\N	\N	\N	0	\N	\N
2	admin1	$2b$12$3zodkqfHd.bujSmwv/LCgOf5iuR/evYxI.YZAZ4lw0GO4ROuQkVXW	ADMIN	admin@clari5pay.io	Priya Mehta	+91 98000 22222	t	2025-03-10	\N	\N	\N	\N	\N	\N	0	\N	\N
3	admin2	$2b$12$j/4PAMY6NCVydDpsml2L9OGEpYfad.JsWfSxPLbP2W.bOC4N8.vHy	ADMIN	admin2@clari5pay.io	Rahul Nair	+91 98000 33333	t	2025-05-20	\N	\N	\N	\N	\N	\N	0	\N	\N
4	support1	$2b$12$D0YEI4KicYYTMDKqn0Bm/uDHttFGtzSEEMtt.49ictrbkbXuHkkPm	SUPPORT_AGENT	support@clari5pay.io	Sana Kapoor	+91 98000 44444	t	2025-02-01	\N	\N	\N	\N	\N	\N	0	\N	\N
5	merchant1	$2b$12$wznHGsgbdDLI2qmuwHljVuVfOggjV14zB8cAijea61MHzKY4Jsg0i	MERCHANT	nexus@clari5pay.io	Nexus Fintech Ltd.	+91 90000 12345	t	2025-06-01	2	DEP	WIT	SET	1.5	1.2	485000	LOW	Maker
6	merchant2	$2b$12$SzdPUkyekxLb3ez6li3Of..zzvF8bK1skiDCmbGjBS5VXY.g.BNjm	MERCHANT	bright@clari5pay.io	BrightPay Inc.	+1 415 555 0199	t	2025-07-15	3	BDP	BWI	BST	1.8	1.4	212000	MEDIUM	Checker
7	merchant3	$2b$12$Jfga4iMrgd9CK8cB0wasYOhec0fkpNY4EgP5vxFBwWHwQDjsbt9nO	MERCHANT	zenpay@clari5pay.io	ZenPay Solutions	+91 90000 67890	t	2025-08-02	2	ZDP	ZWI	ZST	1.6	1.3	98000	LOW	Maker
\.


--
-- Name: account_master_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.account_master_id_seq', 3, true);


--
-- Name: account_transaction_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.account_transaction_id_seq', 2, true);


--
-- Name: support_messages_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.support_messages_id_seq', 4, true);


--
-- Name: transactions_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.transactions_id_seq', 7, true);


--
-- Name: users_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.users_id_seq', 7, true);


--
-- Name: account_master account_master_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.account_master
    ADD CONSTRAINT account_master_pkey PRIMARY KEY (id);


--
-- Name: account_transaction account_transaction_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.account_transaction
    ADD CONSTRAINT account_transaction_pkey PRIMARY KEY (id);


--
-- Name: support_messages support_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.support_messages
    ADD CONSTRAINT support_messages_pkey PRIMARY KEY (id);


--
-- Name: transactions transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: ix_account_master_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_account_master_id ON public.account_master USING btree (id);


--
-- Name: ix_account_master_reference_number; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_account_master_reference_number ON public.account_master USING btree (reference_number);


--
-- Name: ix_account_transaction_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_account_transaction_id ON public.account_transaction USING btree (id);


--
-- Name: ix_account_transaction_member_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_account_transaction_member_id ON public.account_transaction USING btree (member_id);


--
-- Name: ix_account_transaction_reference_number; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_account_transaction_reference_number ON public.account_transaction USING btree (reference_number);


--
-- Name: ix_account_transaction_transaction_reference_number; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_account_transaction_transaction_reference_number ON public.account_transaction USING btree (transaction_reference_number);


--
-- Name: ix_support_messages_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_support_messages_id ON public.support_messages USING btree (id);


--
-- Name: ix_support_messages_merchant_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_support_messages_merchant_id ON public.support_messages USING btree (merchant_id);


--
-- Name: ix_transactions_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_transactions_id ON public.transactions USING btree (id);


--
-- Name: ix_transactions_ref; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_transactions_ref ON public.transactions USING btree (ref);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: ix_users_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_users_id ON public.users USING btree (id);


--
-- Name: ix_users_username; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);


--
-- Name: account_transaction account_transaction_reference_number_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.account_transaction
    ADD CONSTRAINT account_transaction_reference_number_fkey FOREIGN KEY (reference_number) REFERENCES public.account_master(reference_number);


--
-- Name: support_messages support_messages_merchant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.support_messages
    ADD CONSTRAINT support_messages_merchant_id_fkey FOREIGN KEY (merchant_id) REFERENCES public.users(id);


--
-- Name: transactions transactions_merchant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_merchant_id_fkey FOREIGN KEY (merchant_id) REFERENCES public.users(id);


--
-- Name: users users_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- PostgreSQL database dump complete
--

\unrestrict u2tFnv44vt4SBlnS5xztGcGDYczH3rmqRP5A7rK5kE8awIbL1ZbvSQsiIrIwI2E

