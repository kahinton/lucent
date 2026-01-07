-- Migration: Add organizations table for multi-tenancy
-- Organizations represent top-level customers (companies or individuals)

-- Create the organizations table
CREATE TABLE IF NOT EXISTS organizations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create index for organization name lookups
CREATE INDEX IF NOT EXISTS idx_organizations_name ON organizations (name);

-- Add trigger for updated_at on organizations
DROP TRIGGER IF EXISTS update_organizations_updated_at ON organizations;
CREATE TRIGGER update_organizations_updated_at
    BEFORE UPDATE ON organizations
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Add organization_id column to users table
ALTER TABLE users 
ADD COLUMN IF NOT EXISTS organization_id UUID;

-- Create index for organization_id on users
CREATE INDEX IF NOT EXISTS idx_users_organization_id 
ON users (organization_id) WHERE organization_id IS NOT NULL;

-- Add foreign key constraint
ALTER TABLE users
DROP CONSTRAINT IF EXISTS fk_users_organization_id;

ALTER TABLE users
ADD CONSTRAINT fk_users_organization_id 
FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE;

-- Comments
COMMENT ON TABLE organizations IS 'Top-level customers (companies or individuals) for multi-tenancy';
COMMENT ON COLUMN organizations.name IS 'Display name of the organization';
COMMENT ON COLUMN users.organization_id IS 'Foreign key to organizations table; required for all users';
